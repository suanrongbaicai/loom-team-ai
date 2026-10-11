"""Protected administrator tool: publish loom-onboard v2 + loom-customer-profile v1 and switch scopes to them.

Why a stopped controller: Control.lock is only an in-process lock, and Skills.deploy refuses a workspace whose
skill directory differs from the registered version. Moving the directory and registering the deployment are two
steps, so a task arriving in between could read a half-switched skill. With the owner controller stopped (gateways
keep queueing and retry afterwards) nothing can start a task, so the switch is atomic from the model's view.

No database restore, no skill.loaded audit is ever written here (only a real task's trusted hook writes it), the
old v1 directory and registration stay, and a failure restores the old directories and deployment rows. If that
restoration itself cannot be confirmed, the controller is left stopped and the state says needs-attention.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import sys

SKILLS = (('loom-onboard', 'v2'), ('loom-customer-profile', 'v1'))
ACTIVE = "SELECT count(*) FROM tasks WHERE state IN ('queued','running','preparing','ingesting')"
OPEN_EXECUTIONS = 'SELECT count(*) FROM executions WHERE finished IS NULL'


class RollbackFailed(RuntimeError):
    """The workspaces/registrations could not be confirmed back at v1: never restart the controller on this."""


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


def safe(work, path):
    """Refuse links anywhere on the way: the Work directory itself and every existing component below it."""
    work, path = Path(work), Path(path)
    require(not is_link(work), 'Linked Work directory refused: ' + str(work))
    relative = path.relative_to(work)                             # ValueError if outside the scope's Work directory
    current = work
    for part in relative.parts:
        current = current / part
        require(not (current.exists() or current.is_symlink()) or not is_link(current), 'Linked path component refused: ' + str(current))
    require(path.resolve().is_relative_to(work.resolve()), 'Path leaves the Work directory: ' + str(path))
    return path


def counts(c):
    with c.db.transaction() as db:
        return {
            'tasks': db.execute(ACTIVE).fetchone()[0],
            'executions': db.execute(OPEN_EXECUTIONS).fetchone()[0],
            'loaded_audits': db.execute("SELECT count(*) FROM audit WHERE action='skill.loaded'").fetchone()[0],
            'published_audits': db.execute("SELECT count(*) FROM audit WHERE action='skill.published'").fetchone()[0],
        }


def target_dirs(work):
    return work / '.agents/skills', work / '.agents/skills-staging', work / '.agents/skills-retired'


def physical_groups(homes):
    """Scopes may share one worker home (owner-private and owner-new-private do). Group by the normalised physical Work
    directory so each directory is staged, moved and restored exactly once while every scope is still registered alone."""
    groups = {}
    for scope, work in homes.items():
        key = os.path.normcase(os.path.realpath(work))
        groups.setdefault(key, {'work': work, 'scopes': []})['scopes'].append(scope)
    return groups


def precheck(c, homes, sources):
    """Read-only: everything the switch relies on. Returns the facts it found (also used for the read-only run)."""
    from loom.skills import tree_digest
    with c.db.transaction() as db:
        rows = {(r['id'], r['version']): dict(r) for r in db.execute('SELECT * FROM skills')}
        deployments = [dict(r) for r in db.execute('SELECT * FROM skill_deployments')]
    v1 = rows.get(('loom-onboard', 'v1'))
    require(v1 and v1['state'] == 'active', 'Registered loom-onboard v1 missing')
    digests = {key: tree_digest(sources[key])[0] for key, _ in SKILLS}
    for key, version in SKILLS:
        row = rows.get((key, version))
        require(row is None or row['digest'] == digests[key], 'Registered %s %s differs from the source' % (key, version))
    require(not [r for r in deployments if r['skill'] == 'loom-customer-profile' and r['scope'] in homes],
            'A customer-profile deployment row already exists for a target scope')
    found = {}
    for scope in homes:                                          # every scope, shared directory or not, must start from the same v1 registration
        row = [r for r in deployments if r['scope'] == scope and r['skill'] == 'loom-onboard']
        require(len(row) == 1 and (row[0]['version'], row[0]['digest']) == ('v1', v1['digest']), 'Scope %s is not registered at loom-onboard v1' % scope)
    for key, group in physical_groups(homes).items():
        work = group['work']
        skills_dir, _, _ = target_dirs(work)
        current = safe(work, skills_dir / 'loom-onboard')
        require(current.is_dir() and tree_digest(current)[0] == v1['digest'], 'Work for %s loom-onboard is not the registered v1' % group['scopes'])
        require(not (skills_dir / 'loom-customer-profile').exists(), 'Work for %s already has loom-customer-profile' % group['scopes'])
        for name in ('skills-staging', 'skills-retired'):
            safe(work, work / '.agents' / name)
        for scope in group['scopes']:
            found[scope] = {'onboard_digest': tree_digest(current)[0], 'work': str(work), 'shares_work_with': [x for x in group['scopes'] if x != scope]}
    return {'v1_row': v1, 'registered': sorted('%s@%s' % k for k in rows), 'source_digests': digests, 'scopes': found}


def switch(c, homes, sources, approval, journal_path, stamp, after_scope=None):
    """homes: {scope: Work directory}. sources: {skill: directory with SKILL.md}.
    Raises RollbackFailed if restoration cannot be confirmed; any other exception means everything is back at v1."""
    from loom.skills import Skills, tree_digest
    skills = Skills(c)
    before = counts(c)
    require(before['tasks'] == before['executions'] == 0, 'Active work; do not switch')
    facts = precheck(c, homes, sources)
    groups = physical_groups(homes)
    with c.db.transaction() as db:
        rows = {(r['id'], r['version']): dict(r) for r in db.execute('SELECT * FROM skills')}
    journal = {'stamp': stamp, 'before': before, 'v1_row_before': facts['v1_row'], 'groups': {}, 'scopes': {}, 'published': []}

    def save():
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf8')

    save()
    try:
        for key, version in SKILLS:
            if (key, version) not in rows:                       # never re-publish: it would add a duplicate audit
                skills.publish(key, version, sources[key], approval)
                journal['published'].append([key, version])
                save()
        with c.db.transaction() as db:
            registered = {(r['id'], r['version']): dict(r) for r in db.execute('SELECT * FROM skills')}
        new = registered[('loom-onboard', 'v2')]
        for key, group in groups.items():
            work = group['work']
            skills_dir, staging, retired = target_dirs(work)
            gstep = journal['groups'].setdefault(key, {'scopes': group['scopes']})
            stage = safe(work, staging / ('loom-onboard.' + stamp))
            stage.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(new['path'], stage)
            safe(work, stage)
            require(tree_digest(stage)[0] == new['digest'], 'Staged v2 mismatch')
            old_home = safe(work, retired / ('loom-onboard-v1-' + stamp))
            old_home.parent.mkdir(parents=True, exist_ok=True)
            target = safe(work, skills_dir / 'loom-onboard')
            os.replace(target, old_home)                         # once per physical directory
            gstep['retired'] = True
            save()
            os.replace(stage, target)
            gstep['onboard_switched'] = True
            save()
            require(tree_digest(safe(work, target))[0] == new['digest'], 'Switched v2 mismatch')
            for scope in group['scopes']:                        # each scope is registered and verified on its own
                step = journal['scopes'].setdefault(scope, {'group': key})
                skills.deploy(scope, 'loom-onboard', 'v2', work)
                step['onboard_deployed'] = True
                save()
                skills.deploy(scope, 'loom-customer-profile', 'v1', work)
                step['profile_deployed'] = True
                save()
                if after_scope:
                    after_scope(scope)
        after = counts(c)
        require(after['loaded_audits'] == before['loaded_audits'], 'skill.loaded count changed; never fabricate it')
        require(after['published_audits'] == before['published_audits'] + len(journal['published']), 'Unexpected publish audit count')
        with c.db.transaction() as db:
            v1_after = dict(db.execute("SELECT * FROM skills WHERE id='loom-onboard' AND version='v1'").fetchone())
        require(v1_after == journal['v1_row_before'], 'The v1 registration changed')
        for scope, work in homes.items():
            skills_dir, _, _ = target_dirs(work)
            with c.db.transaction() as db:
                got = {r['skill']: (r['version'], r['digest'], r['stage'])
                       for r in db.execute('SELECT * FROM skill_deployments WHERE scope=?', (scope,))}
            for key, version in SKILLS:
                require(got[key][:2] == (version, registered[(key, version)]['digest']), 'Deployment row mismatch: %s %s' % (scope, key))
                require(tree_digest(safe(work, skills_dir / key))[0] == registered[(key, version)]['digest'], 'Workspace mismatch: %s %s' % (scope, key))
            journal['scopes'][scope]['after'] = {k: list(v) for k, v in got.items()}
        journal['after'] = after
        journal['stage'] = 'switched'
        save()
        return journal
    except BaseException as error:
        journal['error'] = repr(error)
        journal['stage'] = 'rolling-back'
        save()
        try:
            restore(c, homes, journal, stamp)
        except BaseException as failure:
            journal.update(stage='rollback-failed', rollback_error=repr(failure))
            save()
            raise RollbackFailed('Restoration could not be confirmed: ' + repr(failure)) from error
        journal['stage'] = 'rolled-back'
        save()
        raise


def restore(c, homes, journal, stamp):
    """Put every touched physical directory back once, then restore each scope's registration to v1 and drop only the
    deployment rows this run created. Verifies the result; raises if any scope cannot be confirmed."""
    from loom.skills import Skills, tree_digest
    skills = Skills(c)
    with c.db.transaction() as db:
        v1 = dict(db.execute("SELECT * FROM skills WHERE id='loom-onboard' AND version='v1'").fetchone())
    groups = physical_groups(homes)
    for key, gstep in journal['groups'].items():
        work = groups[key]['work']
        skills_dir, _, retired_dir = target_dirs(work)
        profile = safe(work, skills_dir / 'loom-customer-profile')
        if profile.exists():
            shutil.rmtree(profile)
        if gstep.get('retired'):
            target = safe(work, skills_dir / 'loom-onboard')
            retired = safe(work, retired_dir / ('loom-onboard-v1-' + stamp))   # derived from the stamp, never trusted from the file
            if target.exists():
                failed = safe(work, retired_dir / ('loom-onboard-v2-failed-' + stamp))
                os.replace(target, failed)
            os.replace(retired, target)
        require(tree_digest(safe(work, skills_dir / 'loom-onboard'))[0] == v1['digest'], 'Work for %s is not back at v1' % gstep['scopes'])
        require(not (skills_dir / 'loom-customer-profile').exists(), 'Work for %s still has loom-customer-profile' % gstep['scopes'])
    for scope, step in journal['scopes'].items():
        work = homes[scope]
        if step.get('onboard_deployed') or step.get('profile_deployed'):
            skills.deploy(scope, 'loom-onboard', 'v1', work)
            with c.lock, c.db.transaction() as db:
                db.execute("DELETE FROM skill_deployments WHERE scope=? AND skill='loom-customer-profile'", (scope,))
                c.audit(db, 'administrator', None, 'skill.deployment_rolled_back', {'scope': scope, 'stamp': stamp})
        with c.db.transaction() as db:
            row = db.execute("SELECT version,digest FROM skill_deployments WHERE scope=? AND skill='loom-onboard'", (scope,)).fetchone()
            profile_row = db.execute("SELECT 1 FROM skill_deployments WHERE scope=? AND skill='loom-customer-profile'", (scope,)).fetchone()
        require(row and (row['version'], row['digest']) == ('v1', v1['digest']) and not profile_row, 'Scope %s registration not back at v1' % scope)


def controller_state(ops):
    listening, pids = ops.listening(), ops.pids()
    if not listening and not pids:
        return 'stopped'
    if listening and len(pids) == 1:
        return 'running'
    return 'ambiguous'


def execute(config, ops, make_control, snapshot_database, stamp):
    """The stop/switch/start sequence with injectable operations (tests use fakes). Returns the final status dict."""
    folder = config['folder']
    folder.mkdir(parents=True, exist_ok=False)
    before = ops.health()
    status = {'stage': 'prechecked', 'before': before}

    def write():
        (folder / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf8')

    write()
    stop_requested = False
    try:
        require(ops.idle(before) and before['pending'].get('unknown') == 3, 'Not idle or unknown changed; no stop')
        stop_requested = True                                     # set before the call: a timeout can still have stopped it
        ops.stop()
        require(ops.idle(ops.health()) and ops.health()['unknown_ids'] == before['unknown_ids'], 'Work arrived during stop')
        snapshot_database(folder)
        for key, group in physical_groups(config['homes']).items():
            shutil.copytree(group['work'] / '.agents/skills', folder / 'workspace-skills' / '+'.join(group['scopes']))
        c = make_control()
        journal = switch(c, config['homes'], config['sources'], config['approval'], folder / 'journal.json', stamp)
        ops.start()
        after = ops.health()
        require(ops.idle(after) and after['unknown_ids'] == before['unknown_ids'], 'Post-start health differs')
        status.update(stage='switched-controller-running', after=after, journal=str(folder / 'journal.json'), published=journal['published'])
        write()
        return status
    except RollbackFailed as error:
        status.update(stage='needs-attention', error=repr(error),
                      note='Controller deliberately left stopped: workspaces/registrations not confirmed at v1')
        write()
        raise
    except BaseException as error:
        status.update(stage='failed', error=repr(error))
        if stop_requested:
            try:
                state = controller_state(ops)
                if state == 'stopped':
                    ops.start()
                    status['controller'] = 'restarted-after-failure'
                elif state == 'running':
                    status['controller'] = 'still-running'
                else:
                    status.update(stage='needs-attention', controller='ambiguous port/process state; not starting a second one')
            except BaseException as recovery:
                status.update(stage='needs-attention', recovery_error=repr(recovery))
        write()
        raise


class ReadOnlyControl:
    """Just enough of Control for precheck(): a read-only SQLite view, so the read-only run cannot disturb the live controller."""
    def __init__(self, path):
        import contextlib
        import sqlite3
        outer = self

        class _Db:
            @contextlib.contextmanager
            def transaction(self):
                db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
                db.row_factory = sqlite3.Row
                try:
                    yield db
                finally:
                    db.close()
        self.db = _Db()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--framework', type=Path, required=True)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--onboard', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--expect-onboard', help='tree_digest the wrapper pinned for the v2 source; required with --execute')
    parser.add_argument('--expect-profile', help='tree_digest the wrapper pinned for the profile source; required with --execute')
    parser.add_argument('--scopes', nargs='+', default=['owner-private', 'owner-new-private', 'member-private'])
    parser.add_argument('--approval', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(args.framework))
    sys.path.insert(0, str(Path(__file__).resolve().parent))      # cutover.py must sit beside this file (or on PYTHONPATH)
    from loom.control import Control
    from loom.skills import tree_digest
    config = json.loads((args.control / 'controller.json').read_bytes().decode('utf-8-sig'))
    homes = {scope: Path(config['scopes'][scope]['worker_home']) / 'Work' for scope in args.scopes}
    sources = {'loom-onboard': args.onboard, 'loom-customer-profile': args.profile}
    digests = {key: tree_digest(path)[0] for key, path in sources.items()}
    facts = precheck(ReadOnlyControl(args.control / 'runtime.sqlite'), homes, sources)   # no write lock while the controller runs
    print(json.dumps({'read_only': not args.execute, 'source_digests': digests, 'homes': {k: str(v) for k, v in homes.items()},
                      'v1_digest': facts['v1_row']['digest'], 'scopes': facts['scopes'], 'registered': facts['registered']}, ensure_ascii=False))
    if not args.execute:
        return
    require(args.expect_onboard == digests['loom-onboard'] and args.expect_profile == digests['loom-customer-profile'],
            'Source digests differ from the pinned values')
    import cutover                                              # reviewed stop/start/health helpers
    import sqlite3
    from contextlib import closing

    def snapshot(folder):
        with closing(sqlite3.connect((args.control / 'runtime.sqlite').as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(folder / 'runtime.sqlite')) as dst:
                src.backup(dst)
                require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'DB backup integrity failed')

    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    result = execute({'folder': args.evidence / ('skills-before-' + stamp), 'homes': homes, 'sources': sources, 'approval': args.approval},
                     cutover, lambda: Control(args.control), snapshot, stamp)
    print(json.dumps({'ok': True, 'after': result['after'], 'published': result['published']}))


if __name__ == '__main__':
    main()
