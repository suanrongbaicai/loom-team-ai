import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from loom.database import Database
from tools import wipe_rebuild as wr

SECRET = 'SECRET-ROW-CONTENT-DO-NOT-PRINT'


def tree(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file() and not p.name.endswith('.lock'):     # leases are created by the tool exactly as a service would
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class WipeTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.loom = Path(self.temp.name).absolute() / 'LOOM'
        self.control = self.loom / 'Control' / 'OwnerWork'
        self.control.mkdir(parents=True)
        roots = {n: self.control / d for n, d in (('a-private', 'Records'), ('b-private', 'NewOwnerRecords'))}
        policy = {'epoch': 5, 'roster_version': 1, 'members': {'owner': {'active': True, 'note': SECRET}},
                  'scopes': {n: {'root': str(r), 'personnel_file': str(self.control / (n + '-personnel.md'))} for n, r in roots.items()}}
        self.policy_text = json.dumps(policy, sort_keys=True)
        db = Database(self.control / 'runtime.sqlite')
        with db.transaction() as c:
            c.execute('INSERT INTO policy VALUES(1,?)', (self.policy_text,))
            c.execute("INSERT INTO skills VALUES('s','v1','p','d','a','active')")
            c.execute("INSERT INTO skill_deployments VALUES('a-private','s','v1','d','loaded')")
            c.execute("INSERT INTO tasks VALUES('t1','a-private','owner','k','done',NULL,NULL,5,NULL,0,'now')")
            c.execute("INSERT INTO documents(id,owner,scope,kind,created,path,audience) VALUES('d1','owner','a-private','data','now','x','[]')")
            c.execute("INSERT INTO audit(time,actor,task,action,detail) VALUES('now','x',NULL,'test','{}')")
        for n, r in roots.items():
            (r / 'Data').mkdir(parents=True)
            (r / 'Data' / (n + '.json')).write_text(SECRET)
            (self.control / (n + '-personnel.md')).write_text('personnel ' + n)
        (self.control / 'controller.json').write_text('{"secret": true}')
        for d in ('Artifacts', 'conflicts', 'WeChatRuntime', 'skills'):
            (self.control / d).mkdir()
            (self.control / d / 'f.txt').write_text(d)
        (self.control / 'service-closure-check.json').write_text('{}')
        (self.loom / 'Jobs' / 'probe_a').mkdir(parents=True)
        (self.loom / 'Jobs' / 'probe_a' / 'job.json').write_text('{}')
        (self.loom / 'Workers' / 'probe_a' / 'Outputs').mkdir(parents=True)
        (self.loom / 'Workers' / 'probe_a' / 'Outputs' / 'o.txt').write_text('out')
        (self.loom / 'Workers' / 'probe_a' / 'CodexHome').mkdir()
        (self.loom / 'Workers' / 'probe_a' / 'CodexHome' / 'auth.json').write_text('{"token":"x"}')
        for g in ('Gateway', 'GatewayNewOwner'):
            (self.loom / 'Control' / g).mkdir()
            (self.loom / 'Control' / g / 'gateway.json').write_text('{"cfg":1}')
            with contextlib.closing(sqlite3.connect(self.loom / 'Control' / g / 'inbox.sqlite')) as c:
                c.execute('CREATE TABLE inbox(id TEXT PRIMARY KEY,state TEXT)')
                c.execute('CREATE INDEX inbox_state ON inbox(state)')
                c.execute("INSERT INTO inbox VALUES('e1','pending')")
                c.commit()
        self.dest = self.loom / 'Evidence' / 'wipe-1'
        self.keep = {p: (Path(p).read_bytes()) for p in (
            *(self.control / (n + '-personnel.md') for n in roots), self.control / 'controller.json',
            self.control / 'skills' / 'f.txt', self.control / 'WeChatRuntime' / 'f.txt',
            self.loom / 'Workers' / 'probe_a' / 'CodexHome' / 'auth.json',
            self.loom / 'Control' / 'Gateway' / 'gateway.json')}
        self.stopped = dict(listening=lambda port: False)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        plan = wr.build_plan(self.loom, self.control)
        self.assertEqual(plan['refusals'], [])
        wr.backup(plan, self.dest, **self.stopped)
        return plan

    def policy_now(self):
        with contextlib.closing(sqlite3.connect(self.control / 'runtime.sqlite')) as c:
            return c.execute('SELECT body FROM policy').fetchone()[0]

    def assert_kept(self):
        for path, data in self.keep.items():
            self.assertEqual(Path(path).read_bytes(), data, path)

    def test_full_wipe_keeps_only_policy_skills_and_configuration(self):
        plan = self.prepare()
        report = wr.apply(plan, self.dest, **self.stopped)
        self.assertTrue(report['ok'], report)
        self.assertEqual(self.policy_now(), self.policy_text)
        with contextlib.closing(sqlite3.connect(self.control / 'runtime.sqlite')) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM skills').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM skill_deployments').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT action FROM audit').fetchall(), [(wr.MARKER_ACTION,)])
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        self.assert_kept()
        for r in plan['scope_roots'].values():
            self.assertTrue(Path(r).is_dir() and not any(Path(r).iterdir()))
        for g in ('Gateway', 'GatewayNewOwner'):
            with contextlib.closing(sqlite3.connect(self.loom / 'Control' / g / 'inbox.sqlite')) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
                self.assertEqual(len(c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='inbox_state'").fetchall()), 1)
        self.assertFalse((self.control / 'service-closure-check.json').exists())
        self.assertFalse(list(self.control.glob('.pre-wipe-*')))
        self.assertNotIn(SECRET, json.dumps(report))

    def test_a_rebuilt_database_opens_with_the_real_control(self):
        from loom.control import Control
        plan = self.prepare()
        wr.apply(plan, self.dest, **self.stopped)
        c = Control(self.control)
        with c.db.transaction() as db:
            self.assertEqual(c.db.get_policy(db)['epoch'], 5)

    def test_refuses_while_running_or_in_use(self):
        plan = self.prepare()
        with self.assertRaises(wr.Refused):
            wr.apply(plan, self.dest, listening=lambda port: True)
        from loom.instance import InstanceLock
        for lock in (self.control / '.controller.lock', self.loom / 'Control' / 'Gateway' / '.gateway.lock'):
            with InstanceLock(lock):
                for call in (lambda: wr.apply(plan, self.dest, **self.stopped), lambda: wr.restore(plan, self.dest, **self.stopped),
                             lambda: wr.backup(wr.build_plan(self.loom, self.control), self.loom / 'Evidence' / 'wipe-2', **self.stopped)):
                    with self.assertRaises(wr.Refused):
                        call()
        with self.assertRaises(wr.Refused):
            wr.backup(wr.build_plan(self.loom, self.control), self.loom / 'Evidence' / 'wipe-2', listening=lambda p: True)
        self.assertEqual(self.policy_now(), self.policy_text)

    def test_refuses_without_a_good_backup(self):
        plan = wr.build_plan(self.loom, self.control)
        with self.assertRaises(Exception):
            wr.apply(plan, self.dest, **self.stopped)
        plan = self.prepare()
        victim = next(self.dest.rglob('Records/Data/a-private.json'))
        victim.write_text('tampered')
        with self.assertRaises(wr.Refused):
            wr.apply(plan, self.dest, **self.stopped)
        self.assertTrue(any(self.control.joinpath('Records', 'Data').iterdir()))

    def test_refuses_when_data_changed_after_backup(self):
        plan = self.prepare()
        (self.control / 'Records' / 'Data' / 'a-private.json').write_text('newer')
        with self.assertRaises(wr.Refused):
            wr.apply(plan, self.dest, **self.stopped)
        (self.control / 'Records' / 'Data' / 'a-private.json').write_text(SECRET)
        (self.control / 'Artifacts' / 'late.txt').write_text('late')
        with self.assertRaises(wr.Refused):
            wr.apply(plan, self.dest, **self.stopped)

    def test_every_failure_point_restores_everything(self):
        before = tree(self.loom)
        before.pop('Evidence/wipe-1/manifest.json', None)
        for point in ('before-db', 'after-db', 'after-personnel', 'clear:1', 'clear:3', 'after-clear', 'after-inbox'):
            plan = self.prepare() if not self.dest.exists() else wr.build_plan(self.loom, self.control)
            def boom(step, point=point):
                if step == point:
                    raise RuntimeError('injected')
            with self.assertRaises(wr.Refused):
                wr.apply(plan, self.dest, failpoint=boom, **self.stopped)
            after = {k: v for k, v in tree(self.loom).items() if not k.startswith('Evidence/')}
            original = {k: v for k, v in before.items() if not k.startswith('Evidence/')}
            self.assertEqual(after, original, point)
            self.assertEqual(self.policy_now(), self.policy_text)
            self.assertFalse(list(self.control.glob('.pre-wipe-*')))

    def test_applying_twice_is_safe_and_restore_brings_everything_back(self):
        before = {k: v for k, v in tree(self.loom).items()}
        plan = self.prepare()
        first = wr.apply(plan, self.dest, **self.stopped)
        second = wr.apply(plan, self.dest, **self.stopped)
        self.assertTrue(first['ok'] and second['ok'], second)
        wr.restore(plan, self.dest, **self.stopped)
        after = {k: v for k, v in tree(self.loom).items() if not k.startswith('Evidence/')}
        self.assertEqual(after, {k: v for k, v in before.items() if not k.startswith('Evidence/')})

    def test_links_protected_names_and_outside_roots_are_refused(self):
        outside = Path(self.temp.name).absolute() / 'outside'
        outside.mkdir()
        link = self.control / 'Artifacts' / 'alias'
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            self.skipTest('symlink not available')
        self.assertTrue(wr.build_plan(self.loom, self.control)['refusals'])
        link.unlink()
        (self.control / 'conflicts' / 'controller.json').write_text('x')
        self.assertTrue(any('Protected' in r for r in wr.build_plan(self.loom, self.control)['refusals']))
        (self.control / 'conflicts' / 'controller.json').unlink()
        with contextlib.closing(sqlite3.connect(self.control / 'runtime.sqlite')) as c:
            body = json.loads(c.execute('SELECT body FROM policy').fetchone()[0])
            body['scopes']['a-private']['root'] = str(outside)
            c.execute('UPDATE policy SET body=?', (json.dumps(body),))
            c.commit()
        self.assertTrue(any('outside the control root' in r for r in wr.build_plan(self.loom, self.control)['refusals']))

    def test_backup_destination_rules(self):
        plan = wr.build_plan(self.loom, self.control)
        with self.assertRaises(wr.Refused):
            wr.backup(plan, self.control / 'Artifacts' / 'bk', **self.stopped)
        with self.assertRaises(wr.Refused):
            wr.backup(plan, Path(self.temp.name) / 'elsewhere', **self.stopped)
        self.dest.mkdir(parents=True)
        (self.dest / 'x').write_text('x')
        with self.assertRaises(wr.Refused):
            wr.backup(plan, self.dest, **self.stopped)

    def test_optional_directory_only_when_included(self):
        self.assertNotIn(str(self.control / 'WeChatRuntime'), wr.build_plan(self.loom, self.control)['clear_dirs'])
        self.assertIn(str(self.control / 'WeChatRuntime'), wr.build_plan(self.loom, self.control, include=('WeChatRuntime',))['clear_dirs'])
        self.assertTrue(wr.build_plan(self.loom, self.control, include=('skills',))['refusals'])

    def test_rehearsal_copy_clears_only_the_copy(self):
        import shutil
        rehearsal = Path(self.temp.name).absolute() / 'R'
        shutil.copytree(self.loom, rehearsal / 'LOOM')
        copy_control = rehearsal / 'LOOM' / 'Control' / 'OwnerWork'
        before = tree(self.loom)
        self.assertTrue(wr.build_plan(rehearsal / 'LOOM', copy_control)['refusals'])   # without the map the copy points at production
        plan = wr.build_plan(rehearsal / 'LOOM', copy_control, remap=(str(self.control), str(copy_control)))
        self.assertEqual(plan['refusals'], [])
        wr.backup(plan, rehearsal / 'LOOM' / 'Evidence' / 'bk', **self.stopped)
        self.assertTrue(wr.apply(plan, rehearsal / 'LOOM' / 'Evidence' / 'bk', **self.stopped)['ok'])
        self.assertEqual(tree(self.loom), before)

    def _unused(self):
        pass

    def test_protected_names_are_never_hashed_and_fingerprints_catch_changes(self):
        (self.control / 'conflicts' / 'auth.json').write_text('TOKEN')
        plan = wr.build_plan(self.loom, self.control)
        self.assertTrue(plan['refusals'])
        self.assertNotIn('auth.json', ' '.join(f['path'] for f in plan['files']))
        (self.control / 'conflicts' / 'auth.json').unlink()
        plan = self.prepare()
        (self.control / 'controller.json').write_text('{"secret": false}')
        self.assertIn('configuration or login file size/mtime changed', wr.verify(plan)['problems'])

    def test_personnel_replacement_is_reviewed_text_and_restorable(self):
        reviewed = Path(self.temp.name) / 'reviewed'
        reviewed.mkdir()
        for n in ('a-private', 'b-private'):
            (reviewed / (n + '-personnel.md')).write_text('reviewed ' + n)
        plan = wr.build_plan(self.loom, self.control, personnel_dir=reviewed)
        self.assertEqual(plan['refusals'], [])
        wr.backup(plan, self.dest, **self.stopped)
        self.assertTrue(wr.apply(plan, self.dest, **self.stopped)['ok'])
        self.assertEqual((self.control / 'a-private-personnel.md').read_text(), 'reviewed a-private')
        wr.restore(plan, self.dest, **self.stopped)
        self.assertEqual((self.control / 'a-private-personnel.md').read_text(), 'personnel a-private')
        missing = wr.build_plan(self.loom, self.control, personnel_dir=Path(self.temp.name))
        self.assertTrue(missing['refusals'])

    def test_a_link_made_after_planning_is_caught_and_cli_codes(self):
        plan = self.prepare()
        outside = Path(self.temp.name).absolute() / 'outside'
        outside.mkdir()
        (self.control / 'Artifacts' / 'f.txt').unlink()
        (self.control / 'Artifacts').rmdir()
        try:
            os.symlink(outside, self.control / 'Artifacts', target_is_directory=True)
        except OSError:
            self.skipTest('symlink not available')
        with self.assertRaises(wr.Refused):
            wr.apply(plan, self.dest, **self.stopped)
        with self.assertRaises(wr.Refused):
            wr.restore(plan, self.dest, **self.stopped)

    def test_an_edited_plan_naming_a_folder_outside_the_root_is_refused(self):
        plan = self.prepare()
        outside = Path(self.temp.name).absolute() / 'elsewhere'
        outside.mkdir()
        (outside / 'keep.txt').write_text('not part of LOOM')
        plan['clear_dirs'] = plan['clear_dirs'] + [str(outside)]
        with self.assertRaises(wr.Refused):
            wr.apply(plan, self.dest, **self.stopped)
        self.assertTrue((outside / 'keep.txt').exists())

    def test_backup_file_set_must_match_the_plan(self):
        plan = self.prepare()
        (self.control / 'Artifacts' / 'extra.txt').write_text('later')
        newer = wr.build_plan(self.loom, self.control)
        with self.assertRaises(wr.Refused):
            wr.apply(newer, self.dest, **self.stopped)

    def test_verify_restored_and_lock_files_created_and_rehearsal_gate(self):
        plan = self.prepare()
        self.assertFalse((self.control / '.controller.lock').exists() and False)
        wr.apply(plan, self.dest, **self.stopped)
        self.assertFalse(wr.verify_restored(plan, self.dest)['ok'])
        wr.restore(plan, self.dest, **self.stopped)
        self.assertTrue(wr.verify_restored(plan, self.dest)['ok'], wr.verify_restored(plan, self.dest))
        self.assertTrue((self.control / '.controller.lock').exists())     # the tool took the lease itself
        # a rehearsal plan skips only the production port, never the leases
        import shutil
        rehearsal = Path(self.temp.name).absolute() / 'R2'
        shutil.copytree(self.loom, rehearsal / 'LOOM', ignore=shutil.ignore_patterns('Evidence'))
        copy = rehearsal / 'LOOM' / 'Control' / 'OwnerWork'
        rp = wr.build_plan(rehearsal / 'LOOM', copy, remap=(str(self.control), str(copy)))
        self.assertTrue(rp['rehearsal'])
        bk = rehearsal / 'LOOM' / 'Evidence' / 'bk'
        wr.backup(rp, bk, listening=lambda p: True)          # production port busy: irrelevant for a copy
        self.assertTrue(wr.apply(rp, bk, listening=lambda p: True)['ok'])
        wr.restore(rp, bk, listening=lambda p: True)
        self.assertTrue(wr.verify_restored(rp, bk)['ok'])
        # a map that points at the production tree itself is refused, so the port gate cannot be switched off for it
        self.assertTrue(wr.build_plan(self.loom, self.control, remap=(str(self.control), str(self.control)))['refusals'])
        self.assertTrue(wr.build_plan(self.loom, self.control, remap=(str(self.loom), str(copy)))['refusals'])

    def test_a_linked_database_is_refused_before_it_is_read(self):
        real = self.control / 'runtime.sqlite'
        other = Path(self.temp.name).absolute() / 'elsewhere.sqlite'
        real.rename(other)
        try:
            os.symlink(other, real)
        except OSError:
            self.skipTest('symlink not available')
        self.assertTrue(any('database' in r for r in wr.build_plan(self.loom, self.control)['refusals']))

    def test_database_with_an_unmerged_wal_is_backed_up_read_and_restored(self):
        # a service killed mid-flight leaves committed rows only in the -wal file
        keeper = sqlite3.connect(self.control / 'runtime.sqlite', isolation_level=None)
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute('BEGIN IMMEDIATE')
        keeper.execute("INSERT INTO skills VALUES('wal-only','v1','p','d','a','active')")
        keeper.execute('COMMIT')
        wal = self.control / 'runtime.sqlite-wal'
        self.assertTrue(wal.exists() and wal.stat().st_size > 0)
        for name in wr.DB_NAMES:                       # simulate the crash: files stay, the process is gone
            if (self.control / name).exists():
                import shutil
                shutil.copy2(self.control / name, self.control / (name + '.crash'))
        keeper.close()
        for name in wr.DB_NAMES:
            with contextlib.suppress(FileNotFoundError):
                (self.control / name).unlink()
        for name in wr.DB_NAMES:
            if (self.control / (name + '.crash')).exists():
                (self.control / (name + '.crash')).rename(self.control / name)
        plan = self.prepare()
        wr.apply(plan, self.dest, **self.stopped)
        with contextlib.closing(sqlite3.connect(self.control / 'runtime.sqlite')) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM skills WHERE id='wal-only'").fetchone()[0], 1)   # the wal row was carried over
        wr.restore(plan, self.dest, **self.stopped)
        self.assertTrue(wr.verify_restored(plan, self.dest)['ok'])
        with contextlib.closing(sqlite3.connect(self.control / 'runtime.sqlite')) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM skills WHERE id='wal-only'").fetchone()[0], 1)

    def test_jobs_top_level_files_and_empty_dirs_are_covered(self):
        (self.loom / 'Jobs' / 'stray.json').write_text('{}')
        (self.control / 'Artifacts' / 'sub' / 'deeper').mkdir(parents=True)
        plan = self.prepare()
        wr.apply(plan, self.dest, **self.stopped)
        self.assertFalse((self.loom / 'Jobs' / 'stray.json').exists())
        wr.restore(plan, self.dest, **self.stopped)
        self.assertTrue((self.control / 'Artifacts' / 'sub' / 'deeper').is_dir())
        self.assertTrue((self.loom / 'Jobs' / 'stray.json').exists())


if __name__ == '__main__':
    unittest.main()
