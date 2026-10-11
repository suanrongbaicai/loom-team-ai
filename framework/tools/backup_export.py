"""One-click business-data backup to a removable drive, and its restore (落地日志 0108).

`scan` lists removable/USB drives with free space and the space a backup needs.  `export` copies every business file with
a SHA256 while the services keep running (the database goes through SQLite's online backup, so it is consistent), checks
each copy again, and only then renames the working folder to its final name.  `restore` puts a verified backup back.

Never copied: controller.json, gateway.json, ingress.key, worker-credentials.json, model logins (CodexHome), browser
profiles, logs and caches.  The backup therefore holds business data but no passwords or logins; it is NOT encrypted.
Output and progress carry names, counts and sizes only."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VERSION = 1
DATA_DIRS = ('ActionRecovery', 'Artifacts', 'CommandRuns', 'Images', 'WeChatAttachments', 'WeChatAttachmentsV2',
             'WeChatCaptures', 'Capabilities', 'SkillSources', 'skills', 'conflicts')
NEVER = {'controller.json', 'gateway.json', 'ingress.key', 'worker-credentials.json', 'auth.json', 'config.toml', '.env'}
MARGIN = 1.15
FIXED_EXTRA = 64 * 1024 * 1024
CHUNK = 1 << 20


class Refused(RuntimeError):
    pass


def is_link(path):
    path = Path(path)
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        return bool(getattr(os.lstat(path), 'st_file_attributes', 0) & 0x400)
    except OSError:
        return False


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def paths_for(control):
    control = Path(control)
    return control, control.parent.parent


def collect(control, policy_roots=None):
    """Everything worth backing up as (source path, relative name); links are reported, never followed."""
    control, anc_root = paths_for(control)
    files, skipped, seen = [], [], set()

    def add_tree(base):
        base = Path(base)
        if not base.exists():
            return
        if is_link(base):
            skipped.append(str(base))
            return
        for current, names, entries in os.walk(base, followlinks=False):
            names[:] = [n for n in names if not is_link(Path(current) / n) or skipped.append(str(Path(current) / n))]
            for entry in entries:
                path = Path(current) / entry
                if is_link(path):
                    skipped.append(str(path))
                elif entry.lower() not in NEVER and path not in seen:
                    seen.add(path)
                    files.append((path, path.relative_to(anc_root).as_posix()))

    for name in DATA_DIRS:
        add_tree(control / name)
    for root in (policy_roots or []):
        add_tree(root)
    for personnel in control.glob('*personnel*.md'):
        if personnel.is_file() and not is_link(personnel):
            files.append((personnel, personnel.relative_to(anc_root).as_posix()))
    jobs = anc_root / 'Jobs'
    if jobs.is_dir():
        add_tree(jobs)
    workers = anc_root / 'Workers'
    if workers.is_dir():
        for worker in sorted(workers.iterdir()):
            add_tree(worker / 'Outputs')
    return files, skipped


def databases(control):
    """Live databases that must be copied through SQLite's backup, not as files."""
    control, anc_root = paths_for(control)
    found = []
    if (control / 'runtime.sqlite').is_file():
        found.append((control / 'runtime.sqlite', (control / 'runtime.sqlite').relative_to(anc_root).as_posix()))
    for gateway in sorted(control.parent.glob('Gateway*')):
        if (gateway / 'inbox.sqlite').is_file() and not is_link(gateway / 'inbox.sqlite'):
            found.append((gateway / 'inbox.sqlite', (gateway / 'inbox.sqlite').relative_to(anc_root).as_posix()))
    return found


def policy_roots(control):
    path = Path(control) / 'runtime.sqlite'
    if not path.is_file():
        return []
    with contextlib.closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        row = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()
    return [s['root'] for s in json.loads(row[0]).get('scopes', {}).values() if s.get('root')] if row else []


def estimate(control):
    files, skipped = collect(control, policy_roots(control))
    total = largest = 0
    for path, _ in files:
        with contextlib.suppress(OSError):
            size = path.stat().st_size
            total += size
            largest = max(largest, size)
    for path, _ in databases(control):
        with contextlib.suppress(OSError):
            size = path.stat().st_size + sum(Path(str(path) + s).stat().st_size for s in ('-wal',) if Path(str(path) + s).exists())
            total += size
            largest = max(largest, size)
    return {'files': len(files) + len(databases(control)), 'bytes': total, 'needed_bytes': int(total * MARGIN) + FIXED_EXTRA,
            'largest_file_bytes': largest, 'skipped_links': len(skipped)}


def ps_json(output):
    output = (output or '').strip()
    if not output:
        return []
    data = json.loads(output)
    return data if isinstance(data, list) else [data]


SCAN_SCRIPT = (
    "$r=@();"
    "Get-CimInstance Win32_DiskDrive | Where-Object {$_.InterfaceType -eq 'USB' -or $_.MediaType -eq 'Removable Media'} | ForEach-Object {"
    "$d=$_;Get-CimAssociatedInstance -InputObject $d -ResultClassName Win32_DiskPartition | ForEach-Object {"
    "Get-CimAssociatedInstance -InputObject $_ -ResultClassName Win32_LogicalDisk | ForEach-Object {"
    "$r+=[pscustomobject]@{letter=$_.DeviceID;label=[string]$_.VolumeName;fs=[string]$_.FileSystem;free=[int64]$_.FreeSpace;total=[int64]$_.Size;model=[string]$d.Model}}}};"
    "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' | ForEach-Object {if(-not($r.letter -contains $_.DeviceID)){"
    "$r+=[pscustomobject]@{letter=$_.DeviceID;label=[string]$_.VolumeName;fs=[string]$_.FileSystem;free=[int64]$_.FreeSpace;total=[int64]$_.Size;model='removable'}}};"
    "ConvertTo-Json -Compress -InputObject @($r)")


def scan(control, run_powershell, data_drive=None):
    """Removable/USB volumes except the drive that holds the data itself and the system drive."""
    result = run_powershell(SCAN_SCRIPT)
    drives = []
    skip = {(data_drive or '').upper(), os.environ.get('SystemDrive', 'C:').upper()}
    for item in ps_json(result):
        letter = str(item.get('letter', '')).upper()
        if not letter or letter in skip or item.get('total') in (None, 0):
            continue
        drives.append({'letter': letter, 'label': item.get('label') or '', 'fs': item.get('fs') or '',
                       'free': int(item.get('free') or 0), 'total': int(item.get('total') or 0), 'model': item.get('model') or ''})
    est = estimate(control)
    for drive in drives:
        drive['enough_space'] = drive['free'] >= est['needed_bytes']
        # FAT32 cannot hold a single file of 4 GiB or more; say so before the copy starts rather than failing in the middle
        drive['fs_warning'] = ('FAT32 单个文件不能超过 4GB，数据库超过后无法备份，建议换 exFAT 或 NTFS'
                               if drive['fs'].upper() == 'FAT32' and est['largest_file_bytes'] >= 4 * 2**30 - 1 else '')
    return {'drives': drives, 'estimate': est}


def write_progress(path, **fields):
    if not path:
        return
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(fields, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def copy_one(source, target, tries=3):
    """Copy while hashing; if the source changed during the copy (live file), copy again."""
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(tries):
        before = os.stat(source)
        h = hashlib.sha256()
        with open(source, 'rb') as src, open(target, 'wb') as dst:
            for block in iter(lambda: src.read(CHUNK), b''):
                h.update(block)
                dst.write(block)
        after = os.stat(source)
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            return h.hexdigest(), os.stat(target).st_size, False
    return h.hexdigest(), os.stat(target).st_size, True       # still moving: keep the last copy and say so


def copy_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(Path(source).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)) as src, \
            contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    with contextlib.closing(sqlite3.connect(target)) as check:
        if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Refused('A copied database failed its integrity check')
    return sha_file(target), os.stat(target).st_size


def export(control, destination_root, progress=None, now=time.strftime):
    """destination_root is the drive root (e.g. F:\\) or any existing folder; returns the manifest summary."""
    control, anc_root = paths_for(control)
    dest_root = Path(destination_root)
    if not dest_root.exists():
        raise Refused('The destination is not available')
    if os.path.normcase(os.path.abspath(dest_root)).startswith(os.path.normcase(os.path.abspath(anc_root))):
        raise Refused('The destination may not lie inside the data folder')
    if is_link(dest_root):
        raise Refused('The destination is a link')
    files, skipped = collect(control, policy_roots(control))
    dbs = databases(control)
    need = estimate(control)
    free = shutil.disk_usage(dest_root).free
    if free < need['needed_bytes']:
        raise Refused('Not enough free space: %d bytes free, %d needed' % (free, need['needed_bytes']))
    stamp = now('%Y%m%d-%H%M%S')
    work = dest_root / 'ANC-Backup' / (stamp + '.partial')
    final = dest_root / 'ANC-Backup' / stamp
    if work.exists() or final.exists():
        raise Refused('A backup with this name already exists')
    total = sum(p.stat().st_size for p, _ in files if p.exists()) + sum(p.stat().st_size for p, _ in dbs)
    done, entries, warnings = 0, [], []
    write_progress(progress, state='running', done_bytes=0, total_bytes=total, files=0, message='开始备份')
    try:
        for index, (source, rel) in enumerate(dbs):
            digest, size = copy_database(source, work / 'data' / rel)
            entries.append({'path': rel, 'bytes': size, 'sha256': digest, 'kind': 'database'})
            done += source.stat().st_size
            write_progress(progress, state='running', done_bytes=done, total_bytes=total, files=len(entries), message='正在备份数据库')
        for source, rel in files:
            if not source.exists():
                continue
            digest, size, moving = copy_one(source, work / 'data' / rel)
            if moving:        # keep the warning, but the recorded digest is of the copy itself so it is still checked
                warnings.append('changed while copying: ' + rel)
                digest = sha_file(work / 'data' / rel)
            entries.append({'path': rel, 'bytes': size, 'sha256': digest, 'kind': 'file'})
            done += size
            if len(entries) % 25 == 0 or done >= total:
                write_progress(progress, state='running', done_bytes=done, total_bytes=total, files=len(entries), message='正在复制文件')
        write_progress(progress, state='running', done_bytes=done, total_bytes=total, files=len(entries), message='正在逐个核对')
        for entry in entries:
            if sha_file(work / 'data' / entry['path']) != entry['sha256']:
                raise Refused('A copy does not match what was read: ' + entry['path'])
        manifest = {'version': VERSION, 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'host': socket.gethostname(),
                    'source': {'anc_root': str(anc_root), 'control': str(control)}, 'files': entries,
                    'skipped_links': skipped, 'warnings': warnings,
                    'not_included': sorted(NEVER) + ['CodexHome', 'Browser', 'logs', 'caches'],
                    'encrypted': False}
        (work / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
        (work / '恢复说明.txt').write_text(RESTORE_TEXT, encoding='utf-8')
        os.replace(work, final)
    except BaseException as error:
        shutil.rmtree(work, ignore_errors=True)
        write_progress(progress, state='failed', message=str(error)[:300])
        raise
    summary = {'state': 'done', 'folder': str(final), 'files': len(entries), 'bytes': sum(e['bytes'] for e in entries),
               'warnings': len(warnings), 'message': '备份完成并已逐个核对'}
    write_progress(progress, **summary, done_bytes=done, total_bytes=total)
    return summary


RESTORE_TEXT = """这是 Loom 工作助手的业务数据备份。

里面有：数据库、各会话的记录文件、报表与附件、已发布的技能版本、人员与权限文件。
里面没有：任何密码、模型登录、飞书应用密钥、配置文件（controller.json、gateway.json 等）。
这个备份没有加密，请把 U 盘放在安全的地方。

恢复办法（需要由维护人员做，先停服务）：
  python tools/backup_export.py restore --from <本备份文件夹> --control <控制目录> --apply
它会先核对每个文件的校验值，再把原来的数据挪到旁边保存，不会直接覆盖。
配置和登录需要按安装说明重新准备。
"""


def node_problem(path, stop):
    """Why `path` (or a folder between it and `stop`) may not be read or written through, or None: a link/junction, or a
    file with more than one hard link (a write would change another path too)."""
    path, stop = Path(path), Path(stop)
    for q in [path] + [x for x in path.parents if len(x.parts) > len(stop.parts)]:
        if q.exists() and is_link(q):
            return 'link or junction: ' + q.name
    with contextlib.suppress(OSError):
        if path.is_file() and os.lstat(path).st_nlink > 1:
            return 'hard link'
    return None


def unsafe_entry(rel):
    """Why a manifest path may not be used, or None.  Only plain relative names below the data folder are accepted."""
    if not isinstance(rel, str) or not rel or '\\' in rel or rel.startswith('/') or ':' in rel or '\x00' in rel:
        return 'not a plain relative path'
    if any(part in ('', '.', '..') for part in rel.split('/')):
        return 'contains . or .. segments'
    return None


def inside(base, path):
    base, path = os.path.normcase(os.path.realpath(base)), os.path.normcase(os.path.realpath(path))
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def verify_backup(folder):
    folder = Path(folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    problems = []
    data = folder / 'data'
    for entry in manifest['files']:
        reason = unsafe_entry(entry.get('path'))
        if reason:
            problems.append('unsafe path (%s): %r' % (reason, entry.get('path')))
            continue
        path = data / entry['path']
        if not path.is_file():
            problems.append('missing: ' + entry['path'])
        elif node_problem(path, data) or not inside(data, path):
            problems.append('link or outside the backup (%s): %s' % (node_problem(path, data) or 'outside', entry['path']))
        elif sha_file(path) != entry['sha256']:
            problems.append('altered: ' + entry['path'])
    return manifest, problems


def restore(folder, control, apply=False, gate=None):
    """Put a verified backup back.  Without --apply it only reports.  Existing data is moved aside, never deleted."""
    manifest, problems = verify_backup(folder)
    control, anc_root = paths_for(control)
    plan = {'files': len(manifest['files']), 'problems': problems, 'target': str(anc_root)}
    if problems or not apply:
        return plan
    if gate:
        gate()
    for entry in manifest['files']:      # no part of the target path may be a link or lead outside the data root
        target = anc_root / entry['path']
        if not inside(anc_root, target) or node_problem(target, anc_root):
            raise Refused('Restore target is a link or outside the data folder: ' + entry['path'])
    stash = anc_root / ('.pre-restore-' + time.strftime('%Y%m%d%H%M%S'))
    moved = 0
    for entry in manifest['files']:
        target = anc_root / entry['path']
        if target.exists():
            keep = stash / entry['path']
            keep.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, keep)
            moved += 1
    for suffix in ('-wal', '-shm'):
        for entry in manifest['files']:
            if entry['kind'] == 'database':
                with contextlib.suppress(FileNotFoundError):
                    os.replace(str(anc_root / entry['path']) + suffix, str(stash / entry['path']) + suffix)
    for entry in manifest['files']:
        target = anc_root / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(folder) / 'data' / entry['path'], target)
        if sha_file(target) != entry['sha256']:
            raise Refused('A restored file does not match the backup: ' + entry['path'])
    plan.update(restored=len(manifest['files']), moved_aside=moved, stash=str(stash))
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('estimate', 'export', 'verify', 'restore'))
    parser.add_argument('--control')
    parser.add_argument('--dest')
    parser.add_argument('--progress')
    parser.add_argument('--from', dest='source')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'estimate':
            result = estimate(args.control)
        elif args.command == 'export':
            result = export(args.control, args.dest, args.progress)
        elif args.command == 'verify':
            manifest, problems = verify_backup(args.source)
            result = {'files': len(manifest['files']), 'problems': problems, 'ok': not problems}
        else:
            from anc.instance import InstanceBusy, InstanceLock
            locks = contextlib.ExitStack()

            def gate():
                for lock in [Path(args.control) / '.controller.lock'] + [g / '.gateway.lock' for g in Path(args.control).parent.glob('Gateway*')]:
                    try:
                        locks.enter_context(InstanceLock(lock))
                    except InstanceBusy:
                        raise Refused('A service is still running: ' + lock.name)
            with locks:
                result = restore(args.source, args.control, args.apply, gate)
    except Refused as error:
        print(json.dumps({'state': 'refused', 'message': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get('problems') else 1


if __name__ == '__main__':
    sys.exit(main())
