import json
from pathlib import Path
import tempfile
import time
import unittest

from tests.test_backup_export import BackupTests
from tools import panel_bridge as pb


class PanelUsbTests(BackupTests):
    def setUp(self):
        super().setUp()
        self.work = self.base / 'work'
        self.work.mkdir()
        self.cfg = {'work_dir': str(self.work), 'database': str(self.control / 'runtime.sqlite')}
        self.rows = [{'letter': 'F:', 'label': 'U盘', 'fs': 'exFAT', 'free': 10 * 2**30, 'total': 16 * 2**30, 'model': 'K'}]
        self.run = lambda script: json.dumps(self.rows)
        self.spawned = []

    def test_scan_reports_drives_and_the_space_needed(self):
        found = pb.usb_scan(self.cfg, self.run)
        self.assertEqual([d['letter'] for d in found['drives']], ['F:'])
        self.assertGreater(found['estimate']['needed_bytes'], 0)

    def test_scan_gives_the_live_facts_the_panel_shows(self):
        drive = pb.usb_scan(self.cfg, self.run)['drives'][0]
        self.assertTrue(drive['enough_space'])
        self.assertEqual(drive['fs'], 'exFAT')
        self.assertEqual(drive['fs_warning'], '')
        self.rows[0]['free'] = 100
        self.assertFalse(pb.usb_scan(self.cfg, self.run)['drives'][0]['enough_space'])

    def test_fat32_is_flagged_only_when_a_single_file_would_not_fit(self):
        self.rows[0]['fs'] = 'FAT32'
        from tools import backup_export as be
        real = be.estimate
        try:
            be.estimate = lambda control: dict(real(control), largest_file_bytes=5 * 2**30)
            self.assertIn('FAT32', pb.usb_scan(self.cfg, self.run)['drives'][0]['fs_warning'])
            be.estimate = lambda control: dict(real(control), largest_file_bytes=10)
            self.assertEqual(pb.usb_scan(self.cfg, self.run)['drives'][0]['fs_warning'], '')
        finally:
            be.estimate = real

    def test_no_usb_means_no_drives_and_nothing_starts(self):
        self.rows.clear()
        self.assertEqual(pb.usb_scan(self.cfg, self.run)['drives'], [])
        with self.assertRaises(ValueError):
            pb.usb_export(self.cfg, 'F:', self.run, lambda argv, cwd: self.spawned.append(argv))
        self.assertEqual(self.spawned, [])

    def test_a_too_small_drive_and_an_unknown_drive_are_refused(self):
        self.rows[0]['free'] = 100
        with self.assertRaises(ValueError) as caught:
            pb.usb_export(self.cfg, 'F:', self.run, lambda argv, cwd: self.spawned.append(argv))
        self.assertIn('free space', str(caught.exception))
        self.rows[0]['free'] = 10 * 2**30
        with self.assertRaises(ValueError):
            pb.usb_export(self.cfg, 'Z:', self.run, lambda argv, cwd: self.spawned.append(argv))
        self.assertEqual(self.spawned, [])

    def test_start_spawns_one_detached_export_and_blocks_a_second(self):
        spawn = lambda argv, cwd: self.spawned.append((argv, cwd))
        started = pb.usb_export(self.cfg, 'f:', self.run, spawn)
        self.assertTrue(started['started'])
        argv, cwd = self.spawned[0]
        self.assertIn('export', argv)
        self.assertEqual(argv[argv.index('--dest') + 1], 'F:\\')
        self.assertEqual(argv[argv.index('--control') + 1], str(self.control))
        self.assertEqual(json.loads(Path(started['progress']).read_text(encoding='utf-8'))['state'], 'starting')
        with self.assertRaises(RuntimeError):
            pb.usb_export(self.cfg, 'F:', self.run, spawn)
        self.assertEqual(len(self.spawned), 1)
        # a stale 'running' record from a crashed run does not block forever
        Path(started['progress']).write_text(json.dumps({'state': 'running'}), encoding='utf-8')
        old = time.time() - 4000
        import os
        os.utime(started['progress'], (old, old))
        pb.usb_export(self.cfg, 'F:', self.run, spawn)
        self.assertEqual(len(self.spawned), 2)

    def test_the_data_drive_is_never_offered(self):
        drive = str(self.control)[:2] if str(self.control)[1:2] == ':' else ''
        self.assertEqual(pb.control_of(self.cfg), self.control)


del BackupTests   # only the subclass's own tests run here, not a second copy of the parent's

if __name__ == '__main__':
    unittest.main()
