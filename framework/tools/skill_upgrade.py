"""Protected administrator tool: switch ONE already-deployed skill to another registered version in chosen scopes.

Use it to trial a new method version in a few scopes, widen it later, or switch back to the previous version. A new
version given with --skill-dir is published first (never twice). For each physical Work directory the target copy is
staged beside the live one; the directory swap and the deployment rows of every scope sharing that directory are then
committed inside one write transaction. Rules delivery opens a write transaction as well, so a starting task sees either
the old directory and rows or the new ones. The replaced directory is kept under .agents/skills-retired, in-flight tasks
keep their pinned version, it refuses while any task, execution or reply send is in flight, nothing writes skill.loaded (only a real task's trusted hook does), and on failure every
directory and row this run changed is put back.

    python skill_upgrade.py --framework F --control C --evidence E --key loom-work-records --to v11-knowledge-kinds \
        --scopes owner-private [--skill-dir D --approval "<evidence text>"] [--expect-digest H] [--execute]
Without --execute it only reads and prints the facts."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from contextlib import closing
import datetime


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def is_link(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        attributes = getattr(os.lstat(path), 'st_file_attributes', 0)
    except OSError:
        return False
    return bool(attributes & 0x400)                               # FILE_ATTRIBUTE_REPARSE_POINT: junctions and symlinks


def safe(work, *parts):
    """Refuse links anywhere on the way: the Work directory itself and every existing component below it."""
    work = Path(work)
    require(not is_link(work), 'Linked Work directory refused: ' + str(work))
    current = work
    for part in parts:
        current = current / part
        require(not (current.exists() or current.is_symlink()) or not is_link(current), 'Linked path component refused: ' + str(current))
    return current


def physical(work):
    return os.path.normcase(os.path.realpath(work))


def groups_of(homes):
    """Scopes may share one Work directory; each directory is swapped exactly once while every scope keeps its own row."""
    groups = {}
    for scope, work in homes.items():
        groups.setdefault(physical(work), {'work': Path(work), 'scopes': []})['scopes'].append(scope)
    return groups


def facts(db_path, homes, key, version, source=None, others=None):
    """Read-only. Returns what is registered/deployed now; raises when the target scopes cannot be switched cleanly.
    others: {scope: Work} for configured scopes NOT being switched, so a shared directory is never switched under them."""
    from loom.skills import tree_digest
    with closing(sqlite3.connect(Path(db_path).absolute().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone()
        row = dict(row) if row else None
        deployed = {r['scope']: dict(r) for r in db.execute('SELECT * FROM skill_deployments WHERE skill=?', (key,))}
        from loom.database import busy
        inflight = busy(db)
        loaded = db.execute("SELECT count(*) FROM audit WHERE action='skill.loaded'").fetchone()[0]
        published = db.execute("SELECT count(*) FROM audit WHERE action='skill.published'").fetchone()[0]
    if source is not None:
        digest = tree_digest(source)[0]
        require(row is None or row['digest'] == digest, 'Registered %s %s differs from the source' % (key, version))
    else:
        require(row is not None, '%s %s is not registered; give --skill-dir and --approval to publish it' % (key, version))
        digest = row['digest']
    require(row is None or row['state'] == 'active', '%s %s is not active' % (key, version))
    targets = {physical(work) for work in homes.values()}
    for scope, work in (others or {}).items():
        require(not (scope in deployed and physical(work) in targets),
                'Scope %s shares a Work directory with a target scope; switch both together or neither' % scope)
    scopes = {}
    for scope, work in homes.items():
        old = deployed.get(scope)
        require(old is not None, 'Scope %s has no %s deployment; use skill_add.py for a new skill' % (scope, key))
        target = safe(work, '.agents', 'skills', key)
        require(target.is_dir() and tree_digest(target)[0] == old['digest'], 'Work for %s does not hold its registered %s' % (scope, key))
        scopes[scope] = {'work': str(work), 'from_version': old['version'], 'from_digest': old['digest'],
                         'already': (old['version'], old['digest']) == (version, digest)}
    for group in groups_of(homes).values():
        done = {scopes[s]['already'] for s in group['scopes']}
        require(len(done) == 1, 'Scopes %s share a Work directory but are at different versions' % group['scopes'])
    return {'digest': digest, 'registered': row is not None, 'active_tasks': inflight['tasks'], 'open_executions': inflight['executions'],
            'sending_replies': inflight['replies'],
            'loaded_audits': loaded, 'published_audits': published, 'scopes': scopes}


def _register(c, db, scopes, key, version, digest, before, action='skill.deployed'):
    for scope in scopes:
        db.execute('INSERT OR REPLACE INTO skill_deployments VALUES(?,?,?,?,?)', (scope, key, version, digest, 'deployed'))
        c.audit(db, 'administrator', None, action, {'scope': scope, 'skill': key, 'version': version, 'from': before[scope]})


def _swap(c, group, key, version, path, digest, before, stamp):
    """Stage outside the lock, then move directories and write rows in one transaction; undo the moves if it fails."""
    from loom.skills import tree_digest
    work = group['work']
    target = safe(work, '.agents', 'skills', key)
    stage = safe(work, '.agents', 'skills-staging', '%s.%s' % (key, stamp))
    stage.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, stage)
    require(tree_digest(stage)[0] == digest, 'Staged %s %s mismatch' % (key, version))
    first = before['scopes'][group['scopes'][0]]
    retired = safe(work, '.agents', 'skills-retired', '%s-%s-%s' % (key, first['from_version'], stamp))
    retired.parent.mkdir(parents=True, exist_ok=True)
    moved = False
    try:
        with c.lock, c.db.transaction() as db:
            from loom.database import busy
            require(not any(busy(db).values()), 'Work arrived; do not switch')
            for scope in group['scopes']:
                row = db.execute('SELECT version,digest FROM skill_deployments WHERE scope=? AND skill=?', (scope, key)).fetchone()
                require(row and (row['version'], row['digest']) == (before['scopes'][scope]['from_version'], before['scopes'][scope]['from_digest']),
                        'Deployment of %s changed since the precheck' % scope)
            os.replace(target, retired)
            moved = True
            os.replace(stage, target)
            require(tree_digest(safe(work, '.agents', 'skills', key))[0] == digest, 'Switched %s %s mismatch' % (key, version))
            _register(c, db, group['scopes'], key, version, digest,
                      {s: before['scopes'][s]['from_version'] for s in group['scopes']})
    except BaseException:
        if moved:
            if target.exists():
                os.replace(target, safe(work, '.agents', 'skills-retired', '%s-%s-failed-%s' % (key, version, stamp)))
            os.replace(retired, target)
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return str(retired)


def _restore(c, group, key, before, retired, version, stamp):
    """Undo a committed swap: put the retired directory back and the previous rows, in one transaction."""
    from loom.skills import tree_digest
    work = group['work']
    target = safe(work, '.agents', 'skills', key)
    first = before['scopes'][group['scopes'][0]]
    with c.lock, c.db.transaction() as db:
        os.replace(target, safe(work, '.agents', 'skills-retired', '%s-%s-failed-%s' % (key, version, stamp)))
        os.replace(Path(retired), target)
        require(tree_digest(target)[0] == first['from_digest'], 'Work for %s is not back at %s' % (group['scopes'], first['from_version']))
        for scope in group['scopes']:
            old = before['scopes'][scope]
            _register(c, db, [scope], key, old['from_version'], old['from_digest'], {scope: version}, 'skill.deployment_rolled_back')


def upgrade(c, homes, key, version, source, approval, journal_path, stamp, others=None):
    from loom.skills import Skills, tree_digest
    db_path = c.root / 'runtime.sqlite'
    before = facts(db_path, homes, key, version, source, others)
    require(before['active_tasks'] == before['open_executions'] == before['sending_replies'] == 0, 'Active work; do not switch now')
    journal = {'before': before, 'published': False, 'groups': {}, 'stage': 'prechecked'}

    def save():
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf8')

    save()
    if not before['registered']:                                 # never re-publish: it would add a duplicate audit
        require(source is not None and approval, 'Publishing a new version needs its source and approval evidence')
        Skills(c).publish(key, version, source, approval)
        journal['published'] = True
        save()
    with c.db.transaction() as db:
        row = dict(db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone())
    require(row['digest'] == before['digest'], 'Registered digest changed')
    groups = groups_of(homes)
    try:
        for name, group in groups.items():
            if before['scopes'][group['scopes'][0]]['already']:
                continue
            journal['groups'][name] = {'scopes': group['scopes'], 'retired': _swap(c, group, key, version, row['path'], row['digest'], before, stamp)}
            save()
    except BaseException as error:
        journal.update(error=repr(error), stage='rolling-back')
        save()
        try:
            for name, step in reversed(list(journal['groups'].items())):
                _restore(c, groups[name], key, before, step['retired'], version, stamp)
        except BaseException as failure:
            journal.update(stage='rollback-failed', rollback_error=repr(failure))
            save()
            raise RuntimeError('Restoration could not be confirmed; inspect the journal before starting work: ' + repr(failure)) from error
        journal['stage'] = 'rolled-back'
        save()
        raise
    after = facts(db_path, {}, key, version)
    require(after['loaded_audits'] == before['loaded_audits'], 'skill.loaded count changed; never fabricate it')
    require(after['published_audits'] == before['published_audits'] + (1 if journal['published'] else 0), 'Unexpected publish audit count')
    for scope, work in homes.items():
        with c.db.transaction() as db:
            got = db.execute('SELECT version,digest FROM skill_deployments WHERE scope=? AND skill=?', (scope, key)).fetchone()
        require(got and (got['version'], got['digest']) == (version, row['digest']), 'Deployment row mismatch: ' + scope)
        require(tree_digest(safe(work, '.agents', 'skills', key))[0] == row['digest'], 'Workspace mismatch: ' + scope)
    journal['stage'] = 'switched' if journal['groups'] else 'unchanged'
    journal['after'] = after
    save()
    return journal


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--framework', type=Path, required=True)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--to', required=True, help='target version, new or already registered')
    parser.add_argument('--scopes', nargs='+', required=True, help='name every scope explicitly; there is no default')
    parser.add_argument('--skill-dir', type=Path, help='source of a version that is not registered yet')
    parser.add_argument('--approval', help='approval evidence; required when the version is published by this run')
    parser.add_argument('--expect-digest', help='tree_digest of the target version; required with --execute')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.framework))
    from loom.control import Control
    config = json.loads((args.control / 'controller.json').read_bytes().decode('utf-8-sig'))
    home = lambda scope: Path(config['scopes'][scope]['worker_home']) / 'Work'
    homes = {scope: home(scope) for scope in args.scopes}
    others = {scope: home(scope) for scope in config['scopes'] if scope not in homes and config['scopes'][scope].get('worker_home')}
    found = facts(args.control / 'runtime.sqlite', homes, args.key, args.to, args.skill_dir, others)
    print(json.dumps({'read_only': not args.execute, 'facts': found}, ensure_ascii=False))
    if not args.execute:
        return 0
    require(args.expect_digest == found['digest'], 'Target digest differs from the pinned value')
    args.evidence.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect((args.control / 'runtime.sqlite').as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(args.evidence / 'runtime-before.sqlite')) as dst:
            src.backup(dst)
            require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'DB backup integrity failed')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    journal = upgrade(Control(args.control), homes, args.key, args.to, args.skill_dir, args.approval,
                      args.evidence / 'journal.json', stamp, others)
    print(json.dumps({'ok': True, 'stage': journal['stage'], 'published': journal['published'], 'scopes': list(homes)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
