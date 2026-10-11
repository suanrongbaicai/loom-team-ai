"""Provision a brand-new runtime from the factory package (落地日志 0102).

Creates an empty control root with the real schema, writes the approved policy through the administrator entry
(`Control.set_policy`), publishes every skill in the package under the version `factory-<version>`, deploys each to each
scope's workspace, and creates the scope root directories and personnel files (from `rules/personnel.template.md`) that
the policy names.  It never copies data or configuration with real values: policy, approval text and workspaces are
passed in; credentials and `controller.json`/`gateway.json` are written by the operator from `templates/`.
Refuses an existing runtime, so it cannot overwrite anything."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.control import Control, identifier  # noqa: E402
from loom.skills import Skills, tree_digest  # noqa: E402


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


def chain_link(path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.relative_to(path.anchor).parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and is_link(current):
            return str(current)
    return None


def inside(parent, path):
    parent, path = os.path.normcase(os.path.abspath(parent)), os.path.normcase(os.path.abspath(path))
    return path != parent and path.startswith(parent + os.sep)


def same_or_nested(a, b):
    a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def validate(control_root, policy, approval, skills_dir, version, workspaces, personnel_template):
    """Everything is checked before anything is created; nothing here writes."""
    root = Path(control_root)
    if not root.is_absolute():
        raise Refused('Control root must be an absolute path')
    if (root / 'runtime.sqlite').exists():
        raise Refused('A runtime already exists here; provisioning never overwrites')
    if root.exists() and any(root.iterdir()):
        raise Refused('Control root must be new or empty')
    if not approval or len(approval) < 12:
        raise Refused('A written approval reference is required')
    try:
        identifier('factory-' + version)
    except Exception:
        raise Refused('Version is not a valid identifier')
    scopes = policy.get('scopes', {})
    if not scopes:
        raise Refused('The policy names no scope')
    missing = [s for s in scopes if s not in workspaces]
    if missing:
        raise Refused('Workspace missing for scope: ' + ', '.join(sorted(missing)))
    extra = [s for s in workspaces if s not in scopes]
    if extra:
        raise Refused('Workspace given for an unknown scope: ' + ', '.join(sorted(extra)))
    targets = [('control root', root)]
    scope_roots, personnel, spaces = [], [], []
    for name in sorted(scopes):
        item = scopes[name]
        scope_root = Path(item.get('root', ''))
        if not scope_root.is_absolute() or not inside(root, scope_root):
            raise Refused('Scope root must be an absolute path inside the control root: ' + name)
        scope_roots.append((name, scope_root))
        if item.get('personnel_file'):
            file = Path(item['personnel_file'])
            if not file.is_absolute() or not inside(root, file):
                raise Refused('Personnel file must be an absolute path inside the control root: ' + name)
            personnel.append((name, file))
        workspace = Path(workspaces[name])
        if not workspace.is_absolute():
            raise Refused('Workspace must be an absolute path: ' + name)
        if same_or_nested(workspace, root):
            raise Refused('Workspace may not overlap the control root: ' + name)
        spaces.append((name, workspace))
    everything = [(n, p) for n, p in scope_roots] + [('personnel ' + n, p) for n, p in personnel] + [('workspace ' + n, p) for n, p in spaces]
    for i, (n1, p1) in enumerate(everything):
        for n2, p2 in everything[i + 1:]:
            if same_or_nested(p1, p2) and not (n1.startswith('personnel') or n2.startswith('personnel')):
                raise Refused('Paths overlap: %s and %s' % (n1, n2))
            if os.path.normcase(os.path.abspath(p1)) == os.path.normcase(os.path.abspath(p2)):
                raise Refused('Paths collide: %s and %s' % (n1, n2))
    for name, path in targets + everything:
        link = chain_link(path)
        if link:
            raise Refused('Link on the path of %s: %s' % (name, link))
    skill_dirs = sorted(p for p in Path(skills_dir).iterdir() if p.is_dir()) if Path(skills_dir).is_dir() else []
    if not skill_dirs:
        raise Refused('No skills found')
    for p in skill_dirs:
        try:
            identifier(p.name)
            tree_digest(p)
        except Exception as error:
            raise Refused('Skill %s is not publishable: %s' % (p.name, type(error).__name__))
    if not Path(personnel_template).is_file():
        raise Refused('Personnel template missing')
    for name, workspace in spaces:
        agents = workspace / '.agents' / 'skills'
        clash = [p.name for p in skill_dirs if (agents / p.name).exists()]
        if clash:
            raise Refused('Workspace of %s already holds skill deployments: %s' % (name, ', '.join(clash)))
        if (workspace.exists() and is_link(workspace)) or (agents.parent.exists() and is_link(agents.parent)):
            raise Refused('Workspace content is a link: ' + name)
    return skill_dirs


def provision(control_root, policy, approval, skills_dir, version, workspaces, personnel_template):
    skill_dirs = validate(control_root, policy, approval, skills_dir, version, workspaces, personnel_template)
    root = Path(control_root)
    scopes = policy['scopes']
    template = Path(personnel_template).read_text(encoding='utf-8')
    created_root = not root.exists()
    created = []          # every path this call newly made, removed again (and checked) if anything fails
    try:
        root.mkdir(parents=True, exist_ok=True)
        c = Control(root)
        epoch = c.set_policy(policy, approval)
        skills = Skills(c)
        published = [skills.publish(p.name, 'factory-' + version, p, approval) for p in skill_dirs]
        for scope in sorted(scopes):
            workspace = Path(workspaces[scope])
            for path in (workspace, workspace / '.agents', workspace / '.agents' / 'skills'):
                top = path
                while not top.parent.exists():
                    top = top.parent
                if not path.exists():
                    created.append(top)
                    path.mkdir(parents=True)
            for item in published:
                created.append(workspace / '.agents' / 'skills' / item['skill'])     # recorded before it is made
                skills.deploy(scope, item['skill'], item['version'], workspace)
            scope_root = Path(scopes[scope]['root'])
            if not scope_root.exists():
                scope_root.mkdir(parents=True)
            file = scopes[scope].get('personnel_file')
            if file and not Path(file).exists():
                Path(file).parent.mkdir(parents=True, exist_ok=True)
                Path(file).write_text(template, encoding='utf-8')
        with c.db.transaction() as db:
            counts = {t: db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                      for t in ('skills', 'skill_deployments', 'tasks', 'documents', 'messages', 'outbox')}
    except BaseException as error:
        left = []
        for path in created:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                left.append(str(path))
        if created_root:
            shutil.rmtree(root, ignore_errors=True)
            if root.exists():
                left.append(str(root))
        else:
            for child in list(root.iterdir()):
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
            left += [str(child) for child in root.iterdir()]
        if left:
            raise Refused('Provisioning failed (%r) and could NOT remove everything it created; inspect and remove: %s'
                          % (error, '; '.join(left)))
        raise Refused('Provisioning failed and everything it created was removed (checked): ' + repr(error))
    return {'epoch': epoch, 'skills_published': len(published), 'scopes': sorted(scopes), 'table_counts': counts,
            'version': 'factory-' + version}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', required=True)
    parser.add_argument('--policy', required=True, help='JSON file with the approved policy state')
    parser.add_argument('--approval', required=True)
    parser.add_argument('--skills', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--workspace', action='append', default=[], metavar='SCOPE=PATH')
    parser.add_argument('--personnel-template', required=True)
    args = parser.parse_args(argv)
    try:
        workspaces = dict(item.split('=', 1) for item in args.workspace)
        result = provision(args.control, json.loads(Path(args.policy).read_text(encoding='utf-8-sig')), args.approval,
                           args.skills, args.version, workspaces, args.personnel_template)
    except Refused as error:
        print(json.dumps({'refused': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
