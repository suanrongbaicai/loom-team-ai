import contextlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import unittest.mock

from tools import maintenance


def make(root):
    control = Path(root) / 'LOOM' / 'Control' / 'OwnerWork'
    control.mkdir(parents=True)
    scope_root = Path(root) / 'LOOM' / 'Workers' / 'scope-a'
    scope_root.mkdir(parents=True)
    (scope_root / 'doc-1.md').write_text('original text', encoding='utf-8')
    (control / 'Artifacts' / 'rep1').mkdir(parents=True)
    (control / 'Artifacts' / 'rep1' / 'report.xlsx').write_bytes(b'xlsx-bytes')
    (control / 'controller.json').write_text('{"secret": "never"}', encoding='utf-8')
    with contextlib.closing(sqlite3.connect(control / 'runtime.sqlite')) as db:
        db.execute('create table t(x)')
        db.execute('insert into t values (1)')
        db.execute('create table policy(singleton integer primary key, body text)')
        db.execute('insert into policy values (1, ?)', ('{"scopes": {"a": {"root": %s}}}' % __import__('json').dumps(str(scope_root)),))
        db.commit()
    return control


class MaintenanceTests(unittest.TestCase):
    def test_backup_is_consistent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            first = maintenance.daily_backup(control)
            self.assertEqual(first['databases'], ['Control/OwnerWork/runtime.sqlite'])
            self.assertTrue(maintenance.daily_backup(control)['already_done'])
            with contextlib.closing(sqlite3.connect(Path(first['folder']) / 'data' / 'Control' / 'OwnerWork' / 'runtime.sqlite')) as db:
                self.assertEqual(db.execute('select x from t').fetchone()[0], 1)

    def test_stuck_send_is_a_problem_only_when_old_and_not_final(self):
        import time
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            now = time.time()
            stamp = lambda age: datetime.fromtimestamp(now - age, timezone.utc).isoformat(timespec='seconds')
            with contextlib.closing(sqlite3.connect(control / 'runtime.sqlite')) as db:
                db.execute('create table outbox(id text, task text, state text, created text)')
                db.executemany('insert into outbox values(?,?,?,?)', [
                    ('o-old-sending', 't1', 'sending', stamp(3600)), ('o-new-queued', 't2', 'queued', stamp(60)),
                    ('o-old-sent', 't3', 'sent', stamp(7200)), ('o-parked', 't4', 'needs_reauthorization', stamp(7200)),
                    ('o-old-unknown', 't5', 'unknown', stamp(4000))])
                db.commit()
            got = maintenance.stuck_sends(control, now)
            self.assertEqual(sorted(i['outbox'] for i in got['stuck']), ['o-old-sendin', 'o-old-unknow'])
            self.assertEqual(got['needs_reauthorization'], 1)
            problems = maintenance.health(control, now)['problems']
            self.assertEqual(len([p for p in problems if p.startswith('stuck send')]), 2)

    def test_daily_backup_also_holds_the_files_the_database_only_points_to_and_can_be_restored(self):
        from tools import backup_export
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            got = maintenance.daily_backup(control)
            self.assertEqual(got['files'], 2)                                   # the scope document and the report
            folder = Path(got['folder'])
            manifest, problems = backup_export.verify_backup(folder)
            self.assertEqual(problems, [])
            paths = {e['path'] for e in manifest['files']}
            self.assertIn('Workers/scope-a/doc-1.md', paths)
            self.assertIn('Control/OwnerWork/Artifacts/rep1/report.xlsx', paths)
            self.assertNotIn('Control/OwnerWork/controller.json', paths)       # secrets/config are never part of a backup
            drill = Path(root) / 'drill' / 'Control' / 'OwnerWork'
            drill.mkdir(parents=True)
            plan = backup_export.restore(folder, drill, apply=True)
            self.assertEqual(plan['problems'], [])
            self.assertEqual((Path(root) / 'drill' / 'Workers' / 'scope-a' / 'doc-1.md').read_text(encoding='utf-8'), 'original text')

    def test_a_failing_file_part_still_keeps_the_database_backup_and_is_reported(self):
        from tools import backup_export
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            real = maintenance.sha_file

            def lying(path):
                return 'bad' if str(path).endswith('doc-1.md') else real(path)
            with unittest.mock.patch.object(maintenance, 'sha_file', lying):
                got = maintenance.daily_backup(control)
            self.assertTrue(got['files_error'])
            self.assertEqual(got['files'], 0)
            manifest, problems = backup_export.verify_backup(got['folder'])       # the folder is complete for what it claims
            self.assertEqual(problems, [])
            self.assertEqual([e['kind'] for e in manifest['files']], ['database'])
            self.assertFalse((Path(got['folder']) / 'data' / 'Workers').exists() and any((Path(got['folder']) / 'data' / 'Workers').rglob('*.md')))

    def test_main_reports_a_files_failure_as_a_health_problem(self):
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            with unittest.mock.patch.object(maintenance, 'copy_one', side_effect=OSError('disk full')):
                with contextlib.redirect_stdout(open(os.devnull, 'w')):
                    code = maintenance.main(['--control', str(control)])
            self.assertEqual(code, 1)
            import json
            health = json.loads((control / 'health.json').read_text(encoding='utf-8'))
            self.assertTrue(any('NOT included' in p for p in health['health']['problems']))

    def test_prune_keeps_newest(self):
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            base = Path(root) / 'LOOM' / 'Backups' / 'daily'
            for i in range(5):
                d = base / f'2026010{i + 1}'
                d.mkdir(parents=True)
                os.utime(d, (1, 1))
            self.assertEqual(maintenance.prune_backups(control), ['20260101', '20260102'])
            self.assertEqual(len(list(base.iterdir())), 3)

    def test_rotate_big_log_and_delete_old(self):
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            big = control / 'a.log'
            big.write_bytes(b'x' * (maintenance.LOG_LIMIT + 1))
            err = control.parent / 'Gateway' / 'gateway.err'
            err.parent.mkdir()
            err.write_bytes(b'x' * (maintenance.LOG_LIMIT + 1))
            old = control / 'b.out.20200101000000.old'
            old.write_text('x')
            os.utime(old, (1, 1))
            result = maintenance.rotate_logs(control)
            self.assertEqual(sorted(result['rotated']), sorted([str(big), str(err)]))
            self.assertFalse(big.exists())
            self.assertEqual(result['deleted'], [str(old)])

    def test_team_material_is_never_rotated_or_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            scope_root = Path(root) / 'LOOM' / 'Workers' / 'scope-a'
            inside = control / 'Data' / 'originals' / 'server.log'
            inside.parent.mkdir(parents=True)
            team = [scope_root / 'export.log', inside, control / 'Artifacts' / 'rep1' / 'trace.out']
            for path in team:
                path.write_bytes(b'x' * (maintenance.LOG_LIMIT + 1))
            kept = [scope_root / 'notes.20200101000000.old', control / 'draft.old', control / 'Data' / 'x.20200101000000.old']
            for path in kept:
                path.write_text('x')
                os.utime(path, (1, 1))
            with contextlib.closing(sqlite3.connect(control / 'runtime.sqlite')) as db:
                db.execute('update policy set body=?', ('{"scopes": {"a": {"root": %s}, "b": {"root": %s}}}' % (
                    __import__('json').dumps(str(scope_root)), __import__('json').dumps(str(control / 'Data'))),))
                db.commit()
            result = maintenance.rotate_logs(control)
            self.assertEqual((result['rotated'], result['deleted']), ([], []))
            for path in team + kept:
                self.assertTrue(path.exists(), path)

    def test_health_flags_missing_backup_and_damage(self):
        with tempfile.TemporaryDirectory() as root:
            control = make(root)
            self.assertIn('no database backup in the last 36 hours', maintenance.health(control)['problems'])
            maintenance.daily_backup(control)
            self.assertFalse([p for p in maintenance.health(control)['problems'] if 'backup' in p or 'database' in p])
            (control / 'runtime.sqlite').write_bytes(b'not a database at all' * 100)
            self.assertTrue([p for p in maintenance.health(control)['problems'] if 'database' in p])


if __name__ == '__main__':
    unittest.main()
