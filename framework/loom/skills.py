"""Complete, immutable skill versions with separate approval/deployment/load state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from .control import identifier, within
from .policy import Denied


def tree_digest(root):
    root = Path(root)
    manifest = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise Denied('Linked skill content refused')
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    if 'SKILL.md' not in manifest:
        raise ValueError('SKILL.md missing')
    value = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return value, manifest


class Skills:
    def __init__(self, control):
        self.c = control

    def publish(self, key, version, source, approval):
        """Protected administrator publisher; not a model-exposed operation."""
        identifier(key)
        identifier(version)
        if not approval:
            raise Denied('Specific version approval required')
        expected, manifest = tree_digest(source)
        target = within(self.c.root, f'skills/{key}/{version}')
        if target.exists():
            if tree_digest(target)[0] != expected:
                raise Denied('Published version is immutable')
        else:
            temporary = target.with_name(version + '.' + uuid.uuid4().hex + '.staging')
            shutil.copytree(source, temporary)
            if tree_digest(temporary)[0] != expected:
                raise RuntimeError('Publication bytes mismatch')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, target)
        with self.c.lock, self.c.db.transaction() as db:
            old = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone()
            if old and (old['digest'], old['approval']) != (expected, approval):
                raise Denied('Version already registered with different content or approval')
            db.execute('INSERT OR IGNORE INTO skills VALUES(?,?,?,?,?,?)', (key, version, str(target), expected, approval, 'active'))
            self.c.audit(db, 'administrator', None, 'skill.published', {'skill': key, 'version': version, 'digest': expected, 'files': list(manifest), 'approval': approval})
        return {'skill': key, 'version': version, 'digest': expected, 'path': str(target)}

    def deploy(self, scope, key, version, workspace):
        with self.c.lock, self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone()
            if not row or row['state'] != 'active':
                raise Denied('Approved skill version unavailable')
            target = within(workspace, f'.agents/skills/{key}')
            if target.exists():
                if tree_digest(target)[0] != row['digest']:
                    raise Denied('Deployment differs; explicit version switch required')
            else:
                shutil.copytree(row['path'], target)
            if tree_digest(target)[0] != row['digest']:
                raise RuntimeError('Deployment content mismatch')
            db.execute('INSERT OR REPLACE INTO skill_deployments VALUES(?,?,?,?,?)', (scope, key, version, row['digest'], 'deployed'))
            self.c.audit(db, 'administrator', None, 'skill.deployed', {'scope': scope, 'skill': key, 'version': version})

    def adopt(self, ctx, key, version):
        with self.c.lock, self.c.db.transaction() as db:
            self.c._check(db, ctx, True)
            row = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone()
            if not row or row['state'] != 'active':
                raise Denied('Skill version unavailable')
            old = db.execute('SELECT version FROM skill_uses WHERE task=? AND skill=?', (ctx.task, key)).fetchone()
            if old and old[0] != version:
                raise Denied('In-flight method pinned; explicit approved switch required')
            deployed = db.execute('SELECT * FROM skill_deployments WHERE scope=? AND skill=?', (ctx.scope, key)).fetchone()
            # A continuation keeps its approved method version even when new
            # conversations deploy a later one. Require this task's fresh load
            # receipt; copied adoption state alone is insufficient.
            loaded = any((item.get('skill'), item.get('version'), item.get('digest'))
                         == (key, version, row['digest'])
                         for item in (json.loads(r[0]) for r in db.execute(
                             "SELECT detail FROM audit WHERE task=? AND action='skill.loaded'", (ctx.task,))))
            if not loaded or (not old and (not deployed or deployed['version'] != version or deployed['stage'] != 'loaded')):
                raise Denied('Version not proven loaded in this scope')
            db.execute('INSERT OR IGNORE INTO skill_uses VALUES(?,?,?,?)', (ctx.task, key, version, 'adopted'))
            db.execute("UPDATE skill_uses SET stage='adopted' WHERE task=? AND skill=?", (ctx.task, key))
            self.c.audit(db, ctx.principal, ctx.task, 'skill.adopted', {'skill': key, 'version': version})

    def loaded(self, ctx, key, observed_digest):
        with self.c.lock, self.c.db.transaction() as db:
            self.c._check(db, ctx)
            row = db.execute('SELECT * FROM skill_deployments WHERE scope=? AND skill=?', (ctx.scope, key)).fetchone()
            if not row or row['digest'] != observed_digest:
                raise Denied('Loaded skill content does not match deployment')
            db.execute("UPDATE skill_deployments SET stage='loaded' WHERE scope=? AND skill=?", (ctx.scope, key))
            self.c.audit(db, 'trusted-hook', ctx.task, 'skill.loaded', {'skill': key, 'version': row['version'], 'digest': observed_digest})

    def disable(self, key, version, approval):
        if not approval:
            raise Denied('Disable evidence required')
        with self.c.lock, self.c.db.transaction() as db:
            db.execute("UPDATE skills SET state='disabled' WHERE id=? AND version=?", (key, version))
            self.c.audit(db, 'administrator', None, 'skill.disabled', {'skill': key, 'version': version, 'approval': approval})
