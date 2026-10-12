"""Wipe business data and rebuild an empty runtime, keeping only the bot (落地日志 0088 §五, 0097/0098).

Subcommands: plan | backup | apply | verify | restore.  Backup, apply and restore take the same OS leases the controller and
the gateways hold (`.controller.lock`, `.gateway.lock`) and keep them for the whole run, so a live service makes the tool
refuse and a service cannot start in the middle.  The tool never reads or prints credentials, controller.json, gateway
configuration, worker logins or any row content: paths, counts, sizes and SHA256 only.

Kept verbatim: `policy` row, `skills`, `skill_deployments`, configuration, logins, skill trees.  Cleared: scope roots,
listed runtime directories, Jobs, worker Outputs, gateway inbox rows.  Personnel files are replaced only by reviewed files
passed with --personnel-dir.  The old data lives on only in the verified backup.
"""
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
import traceback

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
from loom.database import LATEST, SCHEMA, Database  # noqa: E402
from loom.instance import InstanceBusy, InstanceLock  # noqa: E402
from loom.policy import Policy  # noqa: E402
from loom.fsguard import chain_link, is_link, sha_file as file_sha  # noqa: E402

DB_NAMES = ('runtime.sqlite', 'runtime.sqlite-wal', 'runtime.sqlite-shm')
KEEP_TABLES = ('policy', 'skills', 'skill_deployments')
DEFAULT_CLEAR = ('ActionRecovery', 'Artifacts', 'CommandRuns', 'Images', 'WeChatAttachments', 'WeChatAttachmentsV2',
                 'WeChatCaptures', 'conflicts', '.test-work')
OPTIONAL_CLEAR = ('WeChatRuntime',)
CLEAR_FILES = ('service-closure-check.json',)
DENY_NAMES = {'controller.json', 'gateway.json', 'ingress.key', 'worker-credentials.json', 'auth.json',
              'config.toml', '.env'}
MARKER_ACTION = 'maintenance.reset'
PHASES = ('backed-up', 'db-swapped', 'cleared', 'inbox-reset', 'verified')


class Refused(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inside(parent, path):
    parent, path = os.path.normcase(os.path.abspath(parent)), os.path.normcase(os.path.abspath(path))
    return path != parent and path.startswith(parent + os.sep)


def list_tree(directory, refusals):
    """Regular files and directories under `directory`, never following links and never opening protected names."""
    files, dirs = [], []
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        path = Path(entry.path)
        if is_link(path):
            refusals.append('Linked entry refused: ' + str(path))
        elif entry.is_dir(follow_symlinks=False):
            dirs.append(path)
            sub_files, sub_dirs = list_tree(path, refusals)
            files += sub_files
            dirs += sub_dirs
        elif entry.is_file(follow_symlinks=False):
            if entry.name.lower() in DENY_NAMES:
                refusals.append('Protected file name inside a cleared directory: ' + str(path))
            else:
                files.append(path)
    return files, dirs


@contextlib.contextmanager
def scratch(loom_root):
    base = Path(loom_root) / '.wipe-tmp'
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        with contextlib.suppress(OSError):
            base.rmdir()


def ro_uri(path):
    return Path(path).absolute().as_uri() + '?mode=ro'


def snapshot_db(db_path, tmp):
    """Consolidated read-only view of a database whose wal is still beside it."""
    for name in DB_NAMES:
        src = Path(db_path).with_name(name)
        if src.exists():
            shutil.copy2(src, Path(tmp) / name)
    return Path(tmp) / 'runtime.sqlite'


def read_kept(db_path, loom_root):
    with scratch(loom_root) as tmp, contextlib.closing(sqlite3.connect(snapshot_db(db_path, tmp))) as db:
        policy = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()
        if not policy:
            raise Refused('Source policy missing; nothing safe to carry over')
        skills = [tuple(r) for r in db.execute('SELECT id,version,path,digest,approval,state FROM skills ORDER BY id,version')]
        deployments = [tuple(r) for r in db.execute('SELECT scope,skill,version,digest,stage FROM skill_deployments ORDER BY scope,skill')]
    return policy[0], skills, deployments


def remapped(path, remap):
    if not remap:
        return str(path)
    old, new = remap
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(old))
    if a == b or a.startswith(b + os.sep):
        return str(Path(new) / Path(os.path.abspath(path)).relative_to(os.path.abspath(old)))
    return str(path)


def stat_key(path):
    info = os.stat(path)
    return [info.st_size, info.st_mtime_ns]


def protected_fingerprint(loom_root, control, gateways):
    """Size and mtime only: proves configuration and logins were not touched without ever reading them."""
    paths = [control / 'controller.json', control.parent / 'worker-credentials.json']
    for gateway in gateways:
        paths += [Path(gateway).parent / 'gateway.json', Path(gateway).parent / 'ingress.key']
    workers = Path(loom_root) / 'Workers'
    if workers.is_dir():
        paths += [w / 'CodexHome' / 'auth.json' for w in sorted(workers.iterdir())]
    return {str(p): stat_key(p) for p in paths if p.is_file()}


def build_plan(loom_root, control, include=(), remap=None, personnel_dir=None):
    """remap=(production_prefix, rehearsal_prefix) is for rehearsal copies only: the copied policy still names production
    paths, and clearing must happen under the copy.  Roots left outside the control root are refused either way."""
    loom_root, control = Path(loom_root), Path(control)
    refusals = []
    for path in (loom_root, control):
        link = chain_link(path)
        if link:
            refusals.append('Link on path chain: ' + link)
    if remap:
        old = remap[0]
        for path in (loom_root, control):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(old)) or inside(old, path):
                refusals.append('Rehearsal map may not point at the production tree itself: ' + str(path))
    for name in DB_NAMES:
        if (control / name).exists() and (chain_link(control / name) or is_link(control / name)):
            refusals.append('Link on database path: ' + name)
    if not (control / 'runtime.sqlite').is_file():
        refusals.append('runtime.sqlite not found under the control root')
    if refusals:
        return {'refusals': refusals}
    policy_body, _, _ = read_kept(control / 'runtime.sqlite', loom_root)
    state = json.loads(policy_body)
    scope_roots = {}
    for name, scope in sorted(state.get('scopes', {}).items()):
        root = Path(remapped(scope['root'], remap))
        if not inside(control, root):
            refusals.append('Scope root outside the control root refused: ' + name)
        scope_roots[name] = str(root)
    unknown = [n for n in include if n not in OPTIONAL_CLEAR]
    if unknown:
        refusals.append('Not an includable directory: ' + ', '.join(unknown))
    clear = [control / n for n in DEFAULT_CLEAR + tuple(n for n in include if n in OPTIONAL_CLEAR)]
    clear += [Path(r) for r in scope_roots.values()]
    entries = []
    jobs = loom_root / 'Jobs'
    if jobs.is_dir():
        clear += [p for p in sorted(jobs.iterdir()) if p.is_dir() and not is_link(p)]
        entries += [p for p in sorted(jobs.iterdir()) if p.is_file() and not is_link(p)]
        refusals += ['Linked entry refused: ' + str(p) for p in jobs.iterdir() if is_link(p)]
    workers = loom_root / 'Workers'
    if workers.is_dir():
        clear += [p / 'Outputs' for p in sorted(workers.iterdir()) if (p / 'Outputs').exists()]
    gateway_dirs = sorted(control.parent.glob('Gateway*'))
    gateways = []
    for directory in gateway_dirs:
        link = chain_link(directory)
        if link:
            refusals.append('Link on gateway path chain: ' + link)
        elif (directory / 'inbox.sqlite').is_file():
            gateways.append(directory / 'inbox.sqlite')
    files, dirs, clear_dirs = [], [], []
    for directory in clear:
        if not inside(loom_root, directory):
            refusals.append('Outside the allowed root: ' + str(directory))
            continue
        link = chain_link(directory)
        if link:
            refusals.append('Link on path chain: ' + link)
            continue
        if not directory.exists():
            continue
        clear_dirs.append(str(directory))
        sub_files, sub_dirs = list_tree(directory, refusals)
        entries += sub_files
        dirs += sub_dirs
    for name in CLEAR_FILES:
        if (control / name).is_file() and not is_link(control / name):
            entries.append(control / name)
    personnel = {n: remapped(s['personnel_file'], remap) for n, s in sorted(state.get('scopes', {}).items()) if s.get('personnel_file')}
    replace = {}
    if personnel_dir:
        for scope, target in personnel.items():
            source = Path(personnel_dir) / Path(target).name
            if not source.is_file() or source.stat().st_size > 65536 or is_link(source):
                refusals.append('Reviewed personnel file missing or too large for ' + scope)
            else:
                replace[target] = {'source': str(source), 'sha256': file_sha(source)}
    db_files = [control / n for n in DB_NAMES if (control / n).is_file()]
    gw_files = [Path(str(g) + s) for g in gateways for s in ('', '-wal', '-shm') if Path(str(g) + s).is_file()]
    for path in db_files + gw_files + list(personnel.values()):
        if Path(path).exists() and chain_link(path):
            refusals.append('Link on path chain: ' + chain_link(path))
    for path in sorted(set(entries)):
        files.append({'path': str(path), 'bytes': path.stat().st_size, 'sha256': file_sha(path)})
    return {'refusals': refusals, 'loom_root': str(loom_root), 'control': str(control), 'scope_roots': scope_roots,
            'clear_dirs': clear_dirs, 'empty_dirs': sorted(str(d) for d in dirs), 'files': files,
            'db_files': [str(p) for p in db_files], 'gateway_inboxes': [str(g) for g in gateways],
            'gateway_files': [str(p) for p in gw_files], 'personnel': personnel, 'personnel_replace': replace,
            'protected': protected_fingerprint(loom_root, control, gateways),
            'policy_sha256': sha(policy_body.encode()), 'rehearsal': bool(remap),
            'created': time.strftime('%Y-%m-%dT%H:%M:%S')}


def plan_summary(plan):
    return {'refusals': plan.get('refusals', []), 'files': len(plan.get('files', [])),
            'bytes': sum(f['bytes'] for f in plan.get('files', [])), 'clear_dirs': plan.get('clear_dirs', []),
            'db_files': plan.get('db_files', []), 'gateway_inboxes': plan.get('gateway_inboxes', []),
            'scope_roots': plan.get('scope_roots', {}), 'policy_sha256': plan.get('policy_sha256'),
            'rehearsal': plan.get('rehearsal', False), 'personnel_replaced': sorted(plan.get('personnel_replace', {})), 'protected_files': len(plan.get('protected', {}))}


def rel(plan, path):
    return Path(path).relative_to(plan['loom_root'])


def port_listening(port):
    with socket.socket() as probe:
        probe.settimeout(.5)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def revalidate(plan):
    """Plans are re-checked at every use: a junction created after planning must not redirect anything."""
    problems = []
    if plan.get('refusals'):
        problems += plan['refusals']
    for path in [plan['loom_root'], plan['control'], Path(plan['control']) / '.controller.lock'] + plan['clear_dirs'] + plan['db_files'] + plan['gateway_files'] + list(plan['personnel'].values()):
        link = chain_link(path)
        if link:
            problems.append('Link on path chain: ' + link)
    for directory in plan['clear_dirs']:
        # A saved or hand-edited plan must still name only folders under the LOOM root.
        if not inside(plan['loom_root'], directory):
            problems.append('Outside the allowed root: ' + str(directory))
            continue
        if Path(directory).exists():
            list_tree(directory, problems)
    if problems:
        raise Refused('; '.join(problems))


@contextlib.contextmanager
def gate(plan, ports=(9764,), listening=port_listening):
    """Take the controller and gateway leases (creating the lock file when absent, exactly as a service would) and hold them
    for the whole operation; refuse if any service holds one.  A rehearsal copy has its own leases and may run beside the
    production controller, so the production port is not consulted there; the copy root is proven separate in the plan."""
    if not plan.get('rehearsal'):
        for port in ports:
            if listening(port):
                raise Refused('Controller port is listening; stop services first')
    control = Path(plan['control'])
    locks = [control / '.controller.lock'] + [Path(g).parent / '.gateway.lock' for g in plan['gateway_inboxes']]
    for lock in locks:
        link = chain_link(lock)
        if link:
            raise Refused('Link on lock path chain: ' + link)
    with contextlib.ExitStack() as stack:
        for lock in locks:
            try:
                stack.enter_context(InstanceLock(lock))
            except InstanceBusy:
                raise Refused('A service still holds ' + lock.name + ' in ' + str(lock.parent))
        yield


def write_state(dest, phase):
    path = Path(dest) / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else {'phases': []}
    if phase not in state['phases']:
        state['phases'].append(phase)
    path.write_text(json.dumps(state))


def read_state(dest):
    path = Path(dest) / 'state.json'
    return json.loads(path.read_text())['phases'] if path.exists() else []


def backup(plan, dest, *, listening=port_listening):
    revalidate(plan)
    dest = Path(dest)
    evidence = Path(plan['loom_root']) / 'Evidence'
    if not inside(evidence, dest):
        raise Refused('Backup must live under the Evidence directory of the root')
    link = chain_link(dest)
    if link:
        raise Refused('Link on backup path chain: ' + link)
    for clear in plan['clear_dirs']:
        if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(clear)) or inside(clear, dest):
            raise Refused('Backup destination lies inside a directory to be cleared')
    if dest.exists() and any(dest.iterdir()):
        raise Refused('Backup destination must be new or empty')
    with gate(plan, listening=listening):
        manifest = []
        sources = [f['path'] for f in plan['files']] + plan['db_files'] + plan['gateway_files'] + list(plan['personnel'].values())
        for source in sources:
            if not Path(source).is_file():
                continue
            target = dest / 'data' / rel(plan, source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            a = file_sha(source)
            if a != file_sha(target):
                raise Refused('Backup copy differs from its source: ' + source)
            manifest.append({'path': source, 'bytes': target.stat().st_size, 'sha256': a})
        body, skills, deployments = read_kept(dest / 'data' / rel(plan, plan['control']) / 'runtime.sqlite', plan['loom_root'])
        if sha(body.encode()) != plan['policy_sha256']:
            raise Refused('Policy changed between plan and backup')
        result = {'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'loom_root': plan['loom_root'], 'control': plan['control'],
                  'files': manifest, 'empty_dirs': plan['empty_dirs'], 'policy_sha256': plan['policy_sha256'],
                  'skills': skills, 'deployments': deployments, 'plan': plan_summary(plan),
                  'inbox_sql': inbox_sql(plan['gateway_inboxes'], plan['loom_root'])}
        (dest / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
        write_state(dest, 'backed-up')
    return {'files': len(manifest), 'bytes': sum(m['bytes'] for m in manifest), 'policy_sha256': plan['policy_sha256'],
            'manifest_sha256': file_sha(dest / 'manifest.json')}


def inbox_sql(inboxes, loom_root):
    out = {}
    for inbox in inboxes:
        with scratch(loom_root) as tmp:
            for suffix in ('', '-wal', '-shm'):
                if Path(inbox + suffix).exists():
                    shutil.copy2(inbox + suffix, Path(tmp) / ('inbox.sqlite' + suffix))
            with contextlib.closing(sqlite3.connect(Path(tmp) / 'inbox.sqlite')) as db:
                out[inbox] = {'sql': [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='index', name")],
                              'user_version': db.execute('PRAGMA user_version').fetchone()[0],
                              'journal_mode': db.execute('PRAGMA journal_mode').fetchone()[0]}
    return out


def load_manifest(plan, dest):
    manifest = json.loads((Path(dest) / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['loom_root'] != plan['loom_root'] or manifest['control'] != plan['control']:
        raise Refused('Backup belongs to a different root')
    expected = {f['path'] for f in plan['files']} | set(plan['db_files']) | set(plan['gateway_files']) | set(plan['personnel'].values())
    if {m['path'] for m in manifest['files']} != expected:
        raise Refused('Backup file set differs from the plan; take a new backup')
    for item in manifest['files']:
        copy = Path(dest) / 'data' / Path(item['path']).relative_to(manifest['loom_root'])
        if not copy.is_file() or file_sha(copy) != item['sha256']:
            raise Refused('Backup file missing or altered: ' + item['path'])
    if manifest['policy_sha256'] != plan['policy_sha256']:
        raise Refused('Backup policy differs from the plan')
    return manifest


def check_sources(plan, manifest, phases):
    """Every backed-up file must still be identical, or have been changed by a phase this backup already recorded."""
    known = {m['path'] for m in manifest['files']}
    db_done, cleared = 'db-swapped' in phases, 'cleared' in phases
    inbox_started = 'inbox-reset' in phases or cleared
    for item in manifest['files']:
        path = Path(item['path'])
        if path.parent == Path(plan['control']) and path.name in DB_NAMES and db_done:
            continue
        if path.name.endswith(('inbox.sqlite', 'inbox.sqlite-wal', 'inbox.sqlite-shm')) and inbox_started:
            continue
        if str(path) in plan['personnel_replace'] and db_done:
            continue
        if path.is_file() and file_sha(path) != item['sha256']:
            if cleared and str(path) in {f['path'] for f in plan['files']}:
                continue
            raise Refused('Source changed after the backup was taken: ' + item['path'])
    if not cleared:
        for directory in plan['clear_dirs']:
            if Path(directory).exists():
                extra = [str(p) for p in list_tree(directory, [])[0] if str(p) not in known]
                if extra:
                    raise Refused('New file appeared after the backup: ' + extra[0])
    return db_done


def new_database(plan, manifest, dest):
    control = Path(plan['control'])
    new = control / 'runtime.sqlite.new'
    for suffix in ('', '-wal', '-shm'):
        with contextlib.suppress(FileNotFoundError):
            Path(str(new) + suffix).unlink()
    body, skills, deployments = read_kept(Path(dest) / 'data' / rel(plan, control) / 'runtime.sqlite', plan['loom_root'])
    if sha(body.encode()) != manifest['policy_sha256']:
        raise Refused('Backup policy does not match manifest')
    Policy(json.loads(body))       # the real policy class must accept it
    with contextlib.closing(sqlite3.connect(new, isolation_level=None)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nPRAGMA user_version=%d;\nCOMMIT;' % LATEST)
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO policy(singleton,body) VALUES(1,?)', (body,))
        db.executemany('INSERT INTO skills VALUES(?,?,?,?,?,?)', skills)
        db.executemany('INSERT INTO skill_deployments VALUES(?,?,?,?,?)', deployments)
        db.execute('INSERT INTO audit(time,actor,task,action,detail) VALUES(?,?,?,?,?)',
                   (time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()), 'maintenance', None, MARKER_ACTION,
                    json.dumps({'manifest_sha256': file_sha(Path(dest) / 'manifest.json'), 'policy_sha256': plan['policy_sha256'],
                                'files_backed_up': len(manifest['files'])})))
        db.execute('COMMIT')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Refused('New database failed quick_check')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with contextlib.closing(sqlite3.connect(ro_uri(new), uri=True)) as db:
        db.execute('SELECT COUNT(*) FROM policy')
    Database(new)                  # the real schema check; raises on any difference
    for suffix in ('-wal', '-shm'):
        with contextlib.suppress(FileNotFoundError):
            Path(str(new) + suffix).unlink()
    return new


def swap_database(control, new, stash):
    """One atomic step replaces the main file; the old files are copied to the stash first."""
    for name in DB_NAMES:
        if (control / name).exists():
            shutil.copy2(control / name, stash / name)
    for name in DB_NAMES[1:]:
        with contextlib.suppress(FileNotFoundError):
            (control / name).unlink()
    os.replace(new, control / 'runtime.sqlite')


def reset_inbox(path, spec, stash):
    path = Path(path)
    fresh = path.with_name('inbox.sqlite.new')
    with contextlib.suppress(FileNotFoundError):
        fresh.unlink()
    with contextlib.closing(sqlite3.connect(fresh)) as db:
        if spec['journal_mode'] != 'delete':
            db.execute('PRAGMA journal_mode=' + spec['journal_mode'])
        for statement in spec['sql']:
            db.execute(statement)
        db.execute('PRAGMA user_version=%d' % spec['user_version'])
        db.commit()
    for suffix in ('', '-wal', '-shm'):
        src = Path(str(path) + suffix)
        if src.exists():
            shutil.copy2(src, stash / (path.parent.name + '-' + src.name))
    for suffix in ('-wal', '-shm'):
        with contextlib.suppress(FileNotFoundError):
            Path(str(path) + suffix).unlink()
    os.replace(fresh, path)
    for suffix in ('-wal', '-shm'):
        with contextlib.suppress(FileNotFoundError):
            Path(str(fresh) + suffix).unlink()


def clear_tree(directory, failpoint=None):
    """Delete contents bottom-up, checking every entry for links as it is reached; the root itself stays."""
    directory = Path(directory)
    if is_link(directory):
        raise Refused('Linked directory refused: ' + str(directory))
    for current, names, files in os.walk(directory, topdown=False, followlinks=False):
        for name in files + names:
            entry = Path(current) / name
            if is_link(entry):
                raise Refused('Link appeared during clearing: ' + str(entry))
            if entry.is_dir():
                entry.rmdir()
            else:
                entry.unlink()
                if failpoint:
                    failpoint('cleared-file')


def cleanup_leftovers(control, loom_root):
    for left in list(Path(control).glob('.pre-wipe-*')):
        shutil.rmtree(left)
    for name in ('runtime.sqlite.new', 'runtime.sqlite.new-wal', 'runtime.sqlite.new-shm'):
        with contextlib.suppress(FileNotFoundError):
            (Path(control) / name).unlink()
    with contextlib.suppress(FileNotFoundError, OSError):
        shutil.rmtree(Path(loom_root) / '.wipe-tmp')


def apply(plan, dest, *, failpoint=None, listening=port_listening):
    failpoint = failpoint or (lambda step: None)
    revalidate(plan)
    dest = Path(dest)
    with gate(plan, listening=listening):
        manifest = load_manifest(plan, dest)
        phases = read_state(dest)
        if 'backed-up' not in phases:
            raise Refused('No completed backup is recorded for this destination')
        check_sources(plan, manifest, phases)
        control = Path(plan['control'])
        cleanup_leftovers(control, plan['loom_root'])
        stash = control / ('.pre-wipe-' + time.strftime('%Y%m%d%H%M%S'))
        stash.mkdir()
        try:
            failpoint('before-db')
            if 'db-swapped' not in phases:
                swap_database(control, new_database(plan, manifest, dest), stash)
                write_state(dest, 'db-swapped')
            failpoint('after-db')
            for target, item in plan['personnel_replace'].items():
                tmp = Path(target).with_name(Path(target).name + '.new')
                shutil.copyfile(item['source'], tmp)
                os.replace(tmp, target)
            failpoint('after-personnel')
            deleted = []

            def count_deletions(step):
                deleted.append(step)
                failpoint('clear:%d' % len(deleted))
            for directory in plan['clear_dirs']:
                if Path(directory).exists():
                    clear_tree(directory, count_deletions)
                Path(directory).mkdir(parents=True, exist_ok=True)
            for item in plan['files']:
                p = Path(item['path'])
                if p.parent == control and p.name in CLEAR_FILES and p.exists():
                    p.unlink()
            for item in plan['files']:        # files directly under Jobs
                p = Path(item['path'])
                if p.parent == Path(plan['loom_root']) / 'Jobs' and p.exists():
                    p.unlink()
            write_state(dest, 'cleared')
            failpoint('after-clear')
            for inbox in plan['gateway_inboxes']:
                reset_inbox(inbox, manifest['inbox_sql'][inbox], stash)
            write_state(dest, 'inbox-reset')
            failpoint('after-inbox')
            report = verify(plan, dest, final=False)
            if report['problems']:
                raise Refused('Post-check failed: ' + '; '.join(report['problems']))
        except BaseException as error:
            text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            restore_all(plan, dest, manifest)
            (dest / 'apply-error.txt').write_text(text, encoding='utf-8')
            shutil.rmtree(stash, ignore_errors=True)
            raise Refused('Apply failed and the backup was restored; full text in apply-error.txt: ' + repr(error))
        shutil.rmtree(stash)
        write_state(dest, 'verified')
    report = verify(plan, dest)
    return report


def verify(plan, dest=None, final=True):
    """Read-only whole check of the rebuilt state."""
    control = Path(plan['control'])
    problems, counts = [], {}
    manifest = None
    if dest is not None and (Path(dest) / 'manifest.json').exists():
        manifest = json.loads((Path(dest) / 'manifest.json').read_text(encoding='utf-8'))
    path = control / 'runtime.sqlite'
    if not path.is_file():
        problems.append('runtime.sqlite missing')
    else:
        with contextlib.closing(sqlite3.connect(ro_uri(path), uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                problems.append('quick_check failed')
            if db.execute('PRAGMA user_version').fetchone()[0] != LATEST:
                problems.append('user_version is not %d' % LATEST)
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                counts[name] = db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
            body = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()
            if not body or sha(body[0].encode()) != plan['policy_sha256']:
                problems.append('policy differs from the original')
            wrong = {n: c for n, c in counts.items() if n not in KEEP_TABLES + ('audit',) and c}
            if wrong:
                problems.append('tables not empty: ' + ', '.join(sorted(wrong)))
            audit = db.execute('SELECT action,detail FROM audit').fetchall()
            if len(audit) != 1 or audit[0][0] != MARKER_ACTION:
                problems.append('audit should hold exactly the reset record')
            elif manifest is not None and json.loads(audit[0][1]).get('manifest_sha256') != file_sha(Path(dest) / 'manifest.json'):
                problems.append('reset record does not name this backup')
            if manifest is not None:
                if [tuple(r) for r in db.execute('SELECT id,version,path,digest,approval,state FROM skills ORDER BY id,version')] != [tuple(r) for r in manifest['skills']]:
                    problems.append('skills differ from the backup')
                if [tuple(r) for r in db.execute('SELECT scope,skill,version,digest,stage FROM skill_deployments ORDER BY scope,skill')] != [tuple(r) for r in manifest['deployments']]:
                    problems.append('skill deployments differ from the backup')
    for directory in plan['clear_dirs']:
        if not Path(directory).is_dir():
            problems.append('directory missing: ' + directory)
        elif any(Path(directory).iterdir()):
            problems.append('directory not empty: ' + directory)
    for inbox in plan['gateway_inboxes']:
        if not Path(inbox).is_file():
            problems.append('inbox missing: ' + inbox)
            continue
        with contextlib.closing(sqlite3.connect(ro_uri(inbox), uri=True)) as db:
            sql = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='index', name")]
            if manifest is not None and sql != manifest['inbox_sql'][inbox]['sql']:
                problems.append('inbox structure differs from the backup: ' + inbox)
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                if db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]:
                    problems.append('inbox not empty: ' + inbox)
    after = protected_fingerprint(plan['loom_root'], control, plan['gateway_inboxes'])
    if after != plan['protected']:
        problems.append('configuration or login file size/mtime changed')
    for target in plan['personnel'].values():
        wanted = plan['personnel_replace'].get(target)
        if not Path(target).is_file():
            problems.append('personnel file missing: ' + target)
        elif wanted and file_sha(target) != wanted['sha256']:
            problems.append('personnel file is not the reviewed text: ' + target)
    leftovers = [control / 'runtime.sqlite.new'] + (list(control.glob('.pre-wipe-*')) if final else [])
    problems += ['leftover: ' + str(p) for p in leftovers if p.exists()]
    return {'problems': problems, 'table_counts': counts, 'ok': not problems}


def verify_restored(plan, dest):
    """After a restore: every backed-up file is back byte for byte, the planned directories exist, nothing extra remains."""
    manifest = load_manifest(plan, dest)
    problems = []
    for item in manifest['files']:
        path = Path(item['path'])
        if not path.is_file() or file_sha(path) != item['sha256']:
            problems.append('not restored: ' + item['path'])
    known = {m['path'] for m in manifest['files']}
    for directory in plan['clear_dirs']:
        if not Path(directory).is_dir():
            problems.append('directory missing: ' + directory)
        else:
            problems += ['extra file: ' + str(p) for p in list_tree(directory, problems)[0] if str(p) not in known]
    for directory in manifest['empty_dirs']:
        if not Path(directory).is_dir():
            problems.append('directory missing: ' + directory)
    if protected_fingerprint(plan['loom_root'], Path(plan['control']), plan['gateway_inboxes']) != plan['protected']:
        problems.append('configuration or login file size/mtime changed')
    control = Path(plan['control'])
    problems += ['leftover: ' + str(p) for p in [control / 'runtime.sqlite.new', *control.glob('.pre-wipe-*')] if p.exists()]
    return {'problems': problems, 'files_checked': len(manifest['files']), 'ok': not problems}


def restore_all(plan, dest, manifest):
    """Whole restore from the verified backup; the caller already holds the gate."""
    control = Path(plan['control'])
    for directory in plan['clear_dirs']:
        if Path(directory).exists():
            clear_tree(directory)
        Path(directory).mkdir(parents=True, exist_ok=True)
    for directory in manifest['empty_dirs']:
        Path(directory).mkdir(parents=True, exist_ok=True)
    for name in DB_NAMES:
        with contextlib.suppress(FileNotFoundError):
            (control / name).unlink()
    for name in ('runtime.sqlite.new', 'runtime.sqlite.new-wal', 'runtime.sqlite.new-shm'):
        with contextlib.suppress(FileNotFoundError):
            (control / name).unlink()
    for inbox in plan['gateway_inboxes']:
        for suffix in ('', '-wal', '-shm', '.new', '.new-wal', '.new-shm'):
            with contextlib.suppress(FileNotFoundError):
                Path(inbox + suffix).unlink()
    for item in manifest['files']:
        source = Path(dest) / 'data' / Path(item['path']).relative_to(manifest['loom_root'])
        target = Path(item['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if file_sha(target) != item['sha256']:
            raise Refused('Restored file differs from the backup: ' + item['path'])
    state = Path(dest) / 'state.json'
    state.write_text(json.dumps({'phases': ['backed-up']}))
    return {'restored': len(manifest['files'])}


def restore(plan, dest, *, listening=port_listening):
    revalidate(plan)
    with gate(plan, listening=listening):
        manifest = load_manifest(plan, dest)
        cleanup_leftovers(plan['control'], plan['loom_root'])
        return restore_all(plan, dest, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('plan', 'backup', 'apply', 'verify', 'restore', 'verify-restore'))
    parser.add_argument('--loom-root', required=True)
    parser.add_argument('--control', required=True)
    parser.add_argument('--plan')
    parser.add_argument('--backup')
    parser.add_argument('--include', action='append', default=[])
    parser.add_argument('--personnel-dir', help='folder with the reviewed personnel files, same names as the current ones')
    parser.add_argument('--map-root', nargs=2, metavar=('PRODUCTION', 'COPY'), help='rehearsal copies only')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            plan = build_plan(args.loom_root, args.control, include=args.include, personnel_dir=args.personnel_dir,
                              remap=tuple(args.map_root) if args.map_root else None)
            if args.plan:
                Path(args.plan).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
            print(json.dumps(plan_summary(plan), ensure_ascii=False, indent=1))
            return 1 if plan.get('refusals') else 0
        plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
        if args.command == 'backup':
            result = backup(plan, args.backup)
        elif args.command == 'apply':
            result = apply(plan, args.backup)
        elif args.command == 'verify':
            result = verify(plan, args.backup)
        elif args.command == 'verify-restore':
            result = verify_restored(plan, args.backup)
        else:
            result = restore(plan, args.backup)
    except Refused as error:
        print(json.dumps({'refused': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    sys.exit(main())
