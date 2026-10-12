"""Protected administrator tool: publish ONE new skill and deploy it to chosen scopes (additive; nothing existing changes).

Nothing is replaced or moved (skill_upgrade.py switches an existing skill's version), so the controller keeps running: Skills.deploy copies the skill
into the Work directory and registers the deployment inside one write transaction, so a task never sees one without the
other.  The tool refuses to run while any task, execution or reply send is in flight, takes a database backup first, never writes a
skill.loaded audit (only a real task's trusted hook does), and on failure removes only what this run created.

    python skill_add.py --framework F --control C --evidence E --skill-dir D --key loom-business --version v1 \
        --scopes owner-private owner-new-private member-private --approval "<evidence text>" [--expect-digest H] [--execute]
Without --execute it only reads and prints the facts."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from contextlib import closing


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
    return bool(attributes & 0x400)


def safe_target(work, key):
    work = Path(work)
    require(not is_link(work), 'Linked Work directory refused: ' + str(work))
    current = work
    for part in ('.agents', 'skills', key):
        current = current / part
        require(not (current.exists() or current.is_symlink()) or not is_link(current), 'Linked path component refused: ' + str(current))
    return current


def facts(db_path, homes, key, version, source):
    """Read-only. Returns what is registered/deployed now; raises when the target scopes are not in a clean starting state."""
    from loom.skills import tree_digest
    digest = tree_digest(source)[0]
    with closing(sqlite3.connect(Path(db_path).absolute().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone()
        deployed = {r['scope']: dict(r) for r in db.execute('SELECT * FROM skill_deployments WHERE skill=?', (key,))}
        from loom.database import busy
        inflight = busy(db)
        loaded = db.execute("SELECT count(*) FROM audit WHERE action='skill.loaded'").fetchone()[0]
        published = db.execute("SELECT count(*) FROM audit WHERE action='skill.published'").fetchone()[0]
    require(row is None or row['digest'] == digest, 'Registered %s %s differs from the source' % (key, version))
    scopes = {}
    for scope, work in homes.items():
        target = safe_target(work, key)
        present = target.exists()
        if present:
            require(row is not None and tree_digest(target)[0] == row['digest'], 'Work for %s already has a different %s' % (scope, key))
        old = deployed.get(scope)
        require(old is None or (old['version'], old['digest']) == (version, digest), 'Scope %s already deploys another %s' % (scope, key))
        scopes[scope] = {'work': str(work), 'workspace_has_skill': present, 'deployment_row': bool(old)}
    return {'digest': digest, 'registered': row is not None, 'active_tasks': inflight['tasks'], 'open_executions': inflight['executions'],
            'sending_replies': inflight['replies'],
            'loaded_audits': loaded, 'published_audits': published, 'scopes': scopes}


def add(c, homes, key, version, source, approval, journal_path):
    from loom.skills import Skills, tree_digest
    skills = Skills(c)
    before = facts(c.root / 'runtime.sqlite', homes, key, version, source)
    require(before['active_tasks'] == before['open_executions'] == before.get('sending_replies', 0) == 0, 'Active work; do not deploy now')
    journal = {'before': before, 'created_dirs': [], 'created_rows': [], 'published': False}

    def save():
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf8')

    save()
    try:
        if not before['registered']:                             # never re-publish: it would add a duplicate audit
            skills.publish(key, version, source, approval)
            journal['published'] = True
            save()
        for scope, work in homes.items():
            info = before['scopes'][scope]
            if not info['workspace_has_skill']:
                journal['created_dirs'].append(str(safe_target(work, key)))
            if not info['deployment_row']:
                journal['created_rows'].append(scope)
            save()
            skills.deploy(scope, key, version, work)
        after = facts(c.root / 'runtime.sqlite', {}, key, version, source)
        require(after['loaded_audits'] == before['loaded_audits'], 'skill.loaded count changed; never fabricate it')
        require(after['published_audits'] == before['published_audits'] + (1 if journal['published'] else 0), 'Unexpected publish audit count')
        for scope, work in homes.items():
            with c.db.transaction() as db:
                row = db.execute('SELECT version,digest FROM skill_deployments WHERE scope=? AND skill=?', (scope, key)).fetchone()
            require(row and (row['version'], row['digest']) == (version, before['digest']), 'Deployment row mismatch: ' + scope)
            require(tree_digest(safe_target(work, key))[0] == before['digest'], 'Workspace mismatch: ' + scope)
        journal['stage'] = 'deployed'
        journal['after'] = after
        save()
        return journal
    except BaseException as error:
        journal['error'] = repr(error)
        save()
        for path in journal['created_dirs']:                     # only what this run created
            target = Path(path)
            if target.exists() and not is_link(target):
                shutil.rmtree(target)
        with c.lock, c.db.transaction() as db:
            for scope in journal['created_rows']:
                db.execute('DELETE FROM skill_deployments WHERE scope=? AND skill=?', (scope, key))
            if journal['created_rows']:
                c.audit(db, 'administrator', None, 'skill.deployment_rolled_back', {'skill': key, 'scopes': journal['created_rows']})
        journal['stage'] = 'rolled-back'
        save()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--framework', type=Path, required=True)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--skill-dir', type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--scopes', nargs='+', default=['owner-private', 'owner-new-private', 'member-private'])
    parser.add_argument('--approval', required=True)
    parser.add_argument('--expect-digest')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.framework))
    from loom.control import Control
    config = json.loads((args.control / 'controller.json').read_bytes().decode('utf-8-sig'))
    homes = {scope: Path(config['scopes'][scope]['worker_home']) / 'Work' for scope in args.scopes}
    found = facts(args.control / 'runtime.sqlite', homes, args.key, args.version, args.skill_dir)
    print(json.dumps({'read_only': not args.execute, 'facts': found}, ensure_ascii=False))
    if not args.execute:
        return 0
    require(args.expect_digest == found['digest'], 'Source digest differs from the pinned value')
    args.evidence.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect((args.control / 'runtime.sqlite').as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(args.evidence / 'runtime-before.sqlite')) as dst:
            src.backup(dst)
            require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'DB backup integrity failed')
    journal = add(Control(args.control), homes, args.key, args.version, args.skill_dir, args.approval, args.evidence / 'journal.json')
    print(json.dumps({'ok': True, 'stage': journal['stage'], 'published': journal['published'], 'scopes': list(homes)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
