"""Routine upkeep that needs no human: daily database backup, log rotation, health report (落地日志 0108).

Run it from a daily scheduled task:  python tools/maintenance.py --control <含 runtime.sqlite 的控制目录，现场为 Control\\OwnerWork>
It only reads the databases through SQLite's online backup, never deletes a database backup younger than KEEP_DAYS and
always keeps the newest KEEP_MIN.  The result is written to <control>/health.json and printed; `problems` lists what a
person has to look at (empty means healthy; exit status 1 otherwise)."""
import argparse
import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.backup_export import (NEVER, VERSION, Refused, collect, copy_database, copy_one, databases,  # noqa: E402
                                 policy_roots, sha_file, verify_backup)

KEEP_DAYS = 14
KEEP_MIN = 3
LOG_SUFFIXES = ('.log', '.out', '.err')
LOG_LIMIT = 20 * 1024 * 1024
LOG_KEEP_DAYS = 30
MIN_FREE_BYTES = 5 * 1024 ** 3
BACKUP_MAX_AGE = 36 * 3600


def backups_root(control):
    """<LOOM root>/Backups/daily, beside (not inside) the Control folder, same root the databases are named against."""
    return Path(control).parent.parent / 'Backups' / 'daily'


def backup_days(control):
    root = backups_root(control)
    return sorted(p for p in root.glob('????????') if p.is_dir()) if root.is_dir() else []


def daily_backup(control, now=None):
    """Daily backup in the SAME format as tools/backup_export.py (folder/data/<path> + manifest.json), so `backup_export.py verify|restore --from <folder>` works on it.
    Databases go through SQLite's online backup; the document files, reports/attachments, skills, personnel files and worker outputs that the databases only
    POINT to (the database stores a path and a digest, not the content) are copied and hashed too.  Every copy is re-read and checked before the day's folder
    appears (written as <date>.partial, renamed at the end).  Folders made before 2026-10-05 hold only the databases (old layout, no manifest)."""
    control = Path(control)
    now = now or time.time()
    folder = backups_root(control) / time.strftime('%Y%m%d', time.localtime(now))
    if folder.exists():
        return {'folder': str(folder), 'databases': [], 'files': 0, 'bytes': 0, 'already_done': True}
    tmp = folder.with_name(folder.name + '.partial')
    shutil.rmtree(tmp, ignore_errors=True)
    data = tmp / 'data'
    entries, warnings, done = [], [], []
    for source, rel in databases(control):
        digest, size = copy_database(source, data / rel)
        entries.append({'path': rel, 'bytes': size, 'sha256': digest, 'kind': 'database'})
        done.append(rel)
    skipped, files_error = [], None
    try:                                         # the file part must never cost us the database backup of the day
        files, skipped = collect(control, policy_roots(control))
        kept = []
        for source, rel in files:
            if not Path(source).exists():
                continue
            digest, size, moving = copy_one(Path(source), data / rel)
            if moving:
                warnings.append('changed while copying: ' + rel)
                digest = sha_file(data / rel)
            kept.append({'path': rel, 'bytes': size, 'sha256': digest, 'kind': 'file'})
        for entry in kept:                       # re-read every copy: a backup that was not verified is not a backup
            if sha_file(data / entry['path']) != entry['sha256']:
                raise Refused('A backup copy does not match what was read: ' + entry['path'])
        entries.extend(kept)
    except Exception as error:
        files_error = '%s: %s' % (type(error).__name__, str(error)[:200])
        for rel in [e['path'] for e in entries if e['kind'] == 'file'] + [rel for _, rel in locals().get('files', [])]:
            with contextlib.suppress(OSError):
                (data / rel).unlink()
        entries = [e for e in entries if e['kind'] == 'database']
    for entry in entries:
        if entry['kind'] == 'database' and sha_file(data / entry['path']) != entry['sha256']:
            shutil.rmtree(tmp, ignore_errors=True)
            raise Refused('A database backup copy does not match what was read: ' + entry['path'])
    manifest = {'version': VERSION, 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now)), 'source': {'control': str(control)},
                'files': entries, 'skipped_links': skipped, 'warnings': warnings, 'not_included': sorted(NEVER) + ['CodexHome', 'Browser', 'logs', 'caches'],
                'encrypted': False, 'made_by': 'maintenance.daily_backup', 'files_error': files_error}
    (tmp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.rename(folder)
    return {'folder': str(folder), 'databases': done, 'files': len(entries) - len(done), 'bytes': sum(e['bytes'] for e in entries), 'already_done': False,
            'warnings': len(warnings), 'files_error': files_error}


def prune_backups(control, now=None):
    now = now or time.time()
    removed = []
    for old in backup_days(control)[:-KEEP_MIN]:
        if now - old.stat().st_mtime > KEEP_DAYS * 86400:
            shutil.rmtree(old)
            removed.append(old.name)
    return removed


def rotate_logs(control, now=None):
    now = now or time.time()
    rotated, deleted, locked = [], [], []
    for base in [Path(control)] + sorted(Path(control).parent.glob('Gateway*')):
        for log in sorted(p for p in base.rglob('*') if p.suffix in LOG_SUFFIXES and p.is_file()):
            try:
                if log.stat().st_size <= LOG_LIMIT:
                    continue
                target = log.with_name(f'{log.name}.{time.strftime("%Y%m%d%H%M%S", time.localtime(now))}.old')
                try:
                    log.rename(target)
                except OSError:         # a running service holds the file open: copy it, then empty it in place
                    shutil.copyfile(log, target)
                    with open(log, 'r+b') as stream:
                        stream.truncate(0)
                rotated.append(str(log))
            except OSError:
                locked.append(str(log))
        for old in base.rglob('*.old'):
            with contextlib.suppress(OSError):
                if now - old.stat().st_mtime > LOG_KEEP_DAYS * 86400:
                    old.unlink()
                    deleted.append(str(old))
    return {'rotated': rotated, 'deleted': deleted, 'locked_in_use': locked}


STUCK_SECONDS = 30 * 60
STUCK_STATES = ('queued', 'sending', 'unknown')


def _age(created, now):
    from datetime import datetime, timezone
    try:
        moment = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return now - moment.timestamp()


def stuck_sends(control, now=None):
    """P2-5f: replies that were saved but never reached a final state.  Read-only; counts and ids only, no text.  `needs_reauthorization` is only counted
    (every policy change parks queued replies there; it is not a stuck send), the others are problems once older than STUCK_SECONDS."""
    now = now or time.time()
    path = Path(control) / 'runtime.sqlite'
    if not path.is_file():
        return {'checked': False, 'stuck': [], 'needs_reauthorization': None}
    with contextlib.closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        rows = db.execute("SELECT id,task,state,created FROM outbox WHERE state IN ('queued','sending','unknown','needs_reauthorization')").fetchall()
    stuck, parked = [], 0
    for ident, task, state, created in rows:
        if state == 'needs_reauthorization':
            parked += 1
            continue
        age = _age(created, now)
        if age is None or age > STUCK_SECONDS:
            stuck.append({'outbox': ident[:12], 'task': str(task)[:12], 'state': state, 'age_s': None if age is None else int(age)})
    return {'checked': True, 'stuck': stuck, 'needs_reauthorization': parked}


def health(control, now=None):
    control = Path(control)
    now = now or time.time()
    problems = []
    for source, rel in databases(control):
        try:
            with contextlib.closing(sqlite3.connect(source.absolute().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    problems.append(f'database damaged: {rel}')
        except sqlite3.Error as error:
            problems.append(f'database unreadable: {rel} ({error})')
    free = shutil.disk_usage(control).free
    if free < MIN_FREE_BYTES:
        problems.append(f'disk free below {MIN_FREE_BYTES // 1024 ** 3} GB: {free // 1024 ** 2} MB')
    days = backup_days(control)
    age = None if not days else now - days[-1].stat().st_mtime
    if age is None or age > BACKUP_MAX_AGE:
        problems.append('no database backup in the last 36 hours')
    try:
        sends = stuck_sends(control, now)
    except sqlite3.Error as error:
        sends = {'checked': False, 'stuck': [], 'needs_reauthorization': None}
        problems.append(f'send check failed: {error}')
    for item in sends['stuck']:
        problems.append(f"stuck send: outbox {item['outbox']} task {item['task']} state {item['state']} age {item['age_s']}s")
    return {'checked_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)), 'disk_free_mb': free // 1024 ** 2,
            'newest_backup_age_s': None if age is None else int(age), 'sends': {'checked': sends['checked'], 'stuck': len(sends['stuck']),
                                                                              'needs_reauthorization': sends['needs_reauthorization']}, 'problems': problems}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', required=True)
    args = parser.parse_args(argv)
    result, failed = {}, []
    for name, step in (('backup', daily_backup), ('pruned', prune_backups), ('logs', rotate_logs), ('health', health)):
        try:
            result[name] = step(args.control)
        except Exception as error:  # one failing step must not hide the others
            result[name] = {'error': f'{type(error).__name__}: {error}'}
            failed.append(f'{name} failed: {error}')
    if isinstance(result.get('backup'), dict) and result['backup'].get('files_error'):
        failed.append('backup files NOT included today: ' + result['backup']['files_error'])
    result.setdefault('health', {}).setdefault('problems', []).extend(failed)
    Path(args.control, 'health.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result['health']['problems'] else 0


if __name__ == '__main__':
    sys.exit(main())
