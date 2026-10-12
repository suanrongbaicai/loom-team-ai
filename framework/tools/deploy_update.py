"""Replace framework code on a running installation in one verified, reversible step (落地日志 0109).

Steps: plan (what differs) -> idle gate -> stop services -> back up every file about to change -> copy new files and verify
each by SHA256 -> py_compile -> start services -> wait until they run -> report.  Any failure puts the old files back and
starts the services again.  Never touches data, configuration, credentials or skill versions (use skill_add.py / skill_upgrade.py for skills).
The package is a verified factory package (tools/factory.py) or any directory laid out like the framework."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.fsguard import is_link, sha_file as sha  # noqa: E402

ALLOWED_DIRS = ('loom', 'rules', 'tools', 'templates', 'tests')
ALLOWED_ROOT = ('README.md', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'requirements-feishu-win-py312.lock', 'sdk-download-manifest.json')
SKIP_NAMES = {'__pycache__', '.test-work'}
DEFAULT_SERVICES = ('LOOM-Controller-OwnerWork', 'LOOM-Gateway-Member', 'LOOM-Gateway-NewOwner')


class Refused(RuntimeError):
    pass


def tree(root):
    root = Path(root)
    files = {}
    for name in ALLOWED_DIRS:
        base = root / name
        if base.is_symlink() or (base.exists() and is_link(base)):
            raise Refused('Link at ' + name)
        for path in sorted(base.rglob('*')) if base.is_dir() else []:
            if set(path.relative_to(root).parts) & SKIP_NAMES or path.name.endswith('.pyc'):
                continue
            if is_link(path):
                raise Refused('Link inside ' + path.relative_to(root).as_posix())
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path
    for name in ALLOWED_ROOT:
        if (root / name).is_file() and not is_link(root / name):
            files[name] = root / name
    return files


def plan(source, framework):
    new, live = tree(source), tree(framework)
    changed = [rel for rel in sorted(new) if rel in live and sha(new[rel]) != sha(live[rel])]
    added = [rel for rel in sorted(new) if rel not in live]
    only_live = [rel for rel in sorted(live) if rel not in new]
    return {'changed': changed, 'added': added, 'only_in_live': only_live, 'same': len(new) - len(changed) - len(added)}


def idle_seconds(database):
    """Seconds since the last audit record and the amount of work in flight (tasks, executions, replies); read-only."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from loom.database import busy
    with contextlib.closing(sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        last = db.execute('SELECT MAX(time) FROM audit').fetchone()[0]
        active = sum(busy(db).values())
    if not last:
        return 10 ** 9, active
    from datetime import datetime, timezone
    moment = datetime.fromisoformat(last)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds(), active


def db_version(database):
    with contextlib.closing(sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        return db.execute('PRAGMA user_version').fetchone()[0]


def downgrade_database(source, database, to_version, evidence):
    """After a failed update: save a full copy of the database (never over an existing file), then undo the migrations the new
    code made, using the DOWN steps of the NEW code (it is the one that knows what it added)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('loom_database_new', str(Path(source) / 'loom' / 'database.py'),
                                                  submodule_search_locations=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault('loom_database_new', module)
    spec.loader.exec_module(module)
    target = Path(evidence) / 'db-before-downgrade.sqlite'
    if target.exists():
        raise Refused('Refusing to overwrite an existing database copy')
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=15)) as src, \
            contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    return module.downgrade(database, to_version)


class WindowsServices:
    """Scheduled tasks, stopped and started the way the maintenance windows have always done it."""
    def __init__(self, names=DEFAULT_SERVICES, port=9764):
        self.names, self.port = list(names), port

    def _ps(self, script, timeout=60):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, timeout=timeout)
        if result.returncode:
            raise Refused('Service command failed: ' + result.stderr.decode('utf-8', 'replace')[:200])
        return result.stdout.decode('utf-8', 'replace')

    def stop(self):
        for name in self.names:
            self._ps("Disable-ScheduledTask -TaskName '%s' | Out-Null; Stop-ScheduledTask -TaskName '%s'" % (name, name))

    def start(self):
        for name in self.names:                       # controller first (listed first), then gateways
            self._ps("Enable-ScheduledTask -TaskName '%s' | Out-Null; Start-ScheduledTask -TaskName '%s'" % (name, name))

    def running(self):
        out = self._ps("(Get-ScheduledTask -TaskName %s | ForEach-Object {$_.State}) -join ','" % ','.join("'%s'" % n for n in self.names))
        states = [s.strip() for s in out.strip().split(',') if s.strip()]
        return len(states) == len(self.names) and all(s == 'Running' for s in states)

    def port_open(self):
        import socket
        with socket.socket() as probe:
            probe.settimeout(.5)
            return probe.connect_ex(('127.0.0.1', self.port)) == 0

    def stopped(self):
        return not self.port_open()


def apply(source, framework, evidence, services, database=None, min_idle=600, force=False, wait=90, sleep=time.sleep, now=time.time):
    framework, evidence = Path(framework), Path(evidence)
    version_before = db_version(database) if database and Path(database).is_file() else None
    found = plan(source, framework)
    todo = found['changed'] + found['added']
    if not todo:
        return {'state': 'nothing to do', **found}
    if database and not force:
        idle, active = idle_seconds(database)
        if active or idle < min_idle:
            raise Refused('The system was active %d seconds ago (%d tasks, executions or replies in flight); try later' % (idle, active))
    new = tree(source)
    live = tree(framework)
    if evidence.exists() and any(evidence.iterdir()):
        raise Refused('Evidence folder must be new or empty')
    backed = []
    start_attempted = False
    try:
        services.stop()
        if not services.stopped():
            raise Refused('A service still answers after stop')
        if database and Path(database).is_file():       # a full consistent copy before anything changes; never over a file
            copy = evidence / 'db-before-update.sqlite'
            copy.parent.mkdir(parents=True, exist_ok=True)
            if copy.exists():
                raise Refused('Refusing to overwrite an existing database copy')
            with contextlib.closing(sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=15)) as src, \
                    contextlib.closing(sqlite3.connect(copy)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Refused('Database backup failed its integrity check')
        for rel in found['changed']:
            target = evidence / 'before' / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live[rel], target)
            if sha(target) != sha(live[rel]):
                raise Refused('Backup copy differs: ' + rel)
            backed.append(rel)
        for rel in todo:
            destination = framework / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(new[rel], destination)
            if sha(destination) != sha(new[rel]):
                raise Refused('Copied file differs from the package: ' + rel)
        for rel in todo:
            if rel.endswith('.py'):
                py_compile.compile(str(framework / rel), cfile=str(evidence / 'compile' / (rel.replace('/', '__') + 'c')), doraise=True)
        start_attempted = True
        services.start()
        deadline = now() + wait
        while not (services.running() and services.port_open()):
            if now() > deadline:
                raise Refused('Services did not come back within %d seconds' % wait)
            sleep(2)
    except BaseException as error:
        log = {'error': repr(error), 'restored': [], 'removed': [], 'problems': []}

        def attempt(label, step):
            try:
                return step()
            except BaseException as again:          # keep going: every rollback step is tried and recorded
                log['problems'].append('%s: %r' % (label, again))
                return None

        if start_attempted:        # services may be running a mix of old and new files: stop and confirm before touching files
            attempt('stop before restore', services.stop)
            if attempt('confirm stopped', services.stopped) is not True:
                log['problems'].append('services not confirmed stopped; files NOT restored')
        if not log['problems'] or not start_attempted:
            for rel in backed:
                attempt('restore ' + rel, lambda r=rel: (shutil.copy2(evidence / 'before' / r, framework / r), log['restored'].append(r)))
            for rel in found['added']:
                if rel in todo and (framework / rel).exists():
                    attempt('remove ' + rel, lambda r=rel: ((framework / r).unlink(), log['removed'].append(r)))
        if version_before is not None and start_attempted:     # the new code may have migrated the database: undo it with the files
            def undo_schema():
                current = db_version(database)
                if current > version_before:
                    log['database_downgraded'] = [current, downgrade_database(source, database, version_before, evidence)]
            if not log['problems']:
                attempt('downgrade database', undo_schema)
            else:
                log['problems'].append('database not downgraded because the rollback is incomplete')
        attempt('start services', services.start)
        deadline = now() + wait
        healthy = False
        while now() <= deadline:
            if attempt('health check', lambda: services.running() and services.port_open()):
                healthy = True
                break
            sleep(2)
        log['services_healthy_after_rollback'] = healthy
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'rollback.json').write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding='utf-8')
        raise Refused('Update failed (%r); rollback %s; details in rollback.json'
                      % (error, 'complete, services healthy' if healthy and not log['problems'] else 'INCOMPLETE - inspect now'))
    report = {'state': 'updated', **found, 'applied': todo, 'backup': str(evidence / 'before')}
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('plan', 'apply'))
    parser.add_argument('--source', required=True)
    parser.add_argument('--framework', required=True)
    parser.add_argument('--evidence')
    parser.add_argument('--database', help='the live runtime.sqlite; required for apply (idle check, backup, rollback downgrade)')
    parser.add_argument('--min-idle', type=int, default=600)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--service', action='append')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            print(json.dumps(plan(args.source, args.framework), ensure_ascii=False, indent=1))
            return 0
        if not args.evidence:
            raise Refused('--evidence is required')
        if not args.database or not Path(args.database).is_file():
            raise Refused('--database must name the live runtime.sqlite: without it there is no idle check, backup or rollback downgrade')
        result = apply(args.source, args.framework, args.evidence, WindowsServices(args.service or DEFAULT_SERVICES),
                       args.database, args.min_idle, args.force)
    except Refused as error:
        print(json.dumps({'refused': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
