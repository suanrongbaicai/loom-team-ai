"""Protected administrator tool: remove user-authorised TEST data (0065) from the ANC control database and disk.

Not a general delete API. It works from an explicit seed (the S list built from the reviewed dependency graph),
never from keywords or time ranges, and it is built so that "nothing else changed" is checked, not hoped for:

  plan     read-only. Computes the closure of the seed, every table row and file it would remove, every reason it
           refuses (shared documents, protected objects, unknown-delivery rows, references from kept documents).
  archive  read-only on the database. Writes the rows (JSON lines) and files it would remove, with SHA256 manifest.
  apply    controller must be stopped. Requires an archive whose digests match the current plan. One transaction:
           delete, append one `test_data.purged` audit tombstone (audit rows are never deleted), then compare per-table
           row hashes: after == before - selected exactly (anything else changed, e.g. a kept task, policy, skill or
           an unknown-delivery row, rolls the whole transaction back). Files are removed only after the commit.

Kept things need no list to stay safe: any row not selected must be byte-identical afterwards.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

TABLE_ORDER = ['task_reads', 'citation_batches', 'task_links', 'task_requests', 'handoffs', 'processing', 'deliveries',
               'delivery_retries', 'message_actions', 'unknown_notices', 'file_replies', 'report_artifacts', 'materials',
               'wechat_imports', 'wechat_captures', 'wechat_requests', 'host_commits', 'conflicts', 'leases', 'skill_uses',
               'executions', 'cloud_publications', 'cloud_copies', 'web_publications', 'web_requests', 'outbox', 'commits',
               'documents', 'messages', 'tasks']


class Refused(RuntimeError):
    """The plan is not safe to apply; the message lists why."""


def is_link(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        return bool(getattr(os.lstat(path), 'st_file_attributes', 0) & 0x400)   # Windows reparse point
    except OSError:
        return False


def link_in_chain(root, path):
    """First link/junction/reparse point on the ORIGINAL (unresolved) path, root itself included; None when clean.
    resolve() would silently follow an alias, so the check walks the names as written."""
    root, path = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    if is_link(root):
        return str(root)
    walk = root
    for part in path.relative_to(root).parts:
        walk = walk / part
        if (walk.exists() or walk.is_symlink()) and is_link(walk):
            return str(walk)
    return None


def list_tree(directory, refusals):
    """Every regular file under a directory without following anything: a link inside the tree is refused, not entered."""
    found = []
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        path = Path(entry.path)
        if is_link(path):
            refusals.append('Linked entry refused: ' + str(path))
        elif entry.is_dir(follow_symlinks=False):
            found.extend(list_tree(path, refusals))
        elif entry.is_file(follow_symlinks=False):
            found.append(path)
    return found


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(row):
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)


def table_columns(db, table):
    info = db.execute('PRAGMA table_info(%s)' % table).fetchall()
    keys = [r['name'] for r in sorted((r for r in info if r['pk']), key=lambda r: r['pk'])]
    return [r['name'] for r in info], keys


def all_tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def digest_tables(db):
    """{table: {primary-key tuple: row hash}} for every table, for the before/after comparison."""
    result = {}
    for table in all_tables(db):
        _, keys = table_columns(db, table)
        rows = {}
        for index, row in enumerate(db.execute('SELECT * FROM %s' % table).fetchall()):
            key = tuple(row[k] for k in keys) if keys else ('#', index)
            rows[key] = sha(canonical(row).encode())
        result[table] = rows
    return result


def in_(values):
    values = sorted(values)
    return '(' + ','.join('?' * len(values)) + ')', values


def select(db, table, column, values):
    if not values:
        return []
    marks, args = in_(values)
    return [dict(r) for r in db.execute('SELECT * FROM %s WHERE %s IN %s' % (table, column, marks), args)]


def doc_files(c, db, row):
    """Exactly this document's files: current file, its own revision history, and the staging file each of ITS commits
    would have used (a per-commit name, never a directory glob: neighbours' pending files are not ours)."""
    from anc.control import digest
    path = c._path(db, row)
    files = []
    if path.exists():
        files.append(path)
    history = path.parent / '.history' / path.name
    if history.is_dir():
        files.extend(sorted(p for p in history.rglob('*') if p.is_file()))
    for commit in db.execute('SELECT id FROM commits WHERE document=?', (row['id'],)):
        stage = path.with_name('.' + digest(commit['id'].encode())[:32] + '.pending')
        if stage.is_file():
            files.append(stage)
    return files


def plan(c, seed, output_roots=(), control_root=None):
    """Read-only. Returns {'refusals': [...], 'tables': {table: [row dicts]}, 'files': [paths], 'references': [...]}"""
    tasks = set(seed.get('tasks', []))                 # removed completely (task row and everything it owns)
    partial = set(seed.get('tasks_partial', []))       # task row KEPT (pinned by a shared/protected document or an unknown delivery); only what it exclusively owns goes
    outbox_extra = set(seed.get('outbox_extra', []))   # outbox rows of kept/partial tasks that may go with their removed payload document
    owned = tasks | partial
    base = Path(os.path.abspath(control_root or c.root))   # the PRODUCTION control root: relative artifact paths and conflict intents live here, even when c is a database copy
    messages = set(seed.get('messages', []))
    extra_docs = set(seed.get('documents_extra', []))
    protect = seed.get('protect', {})
    protected_tasks = set(protect.get('tasks', []))
    protected_docs = set(protect.get('documents', []))
    protected_unknown = set(protect.get('outbox_unknown', []))
    allowed_references = set(seed.get('allow_references_from', []))
    refusals, references = [], []
    with c.db.transaction() as db:
        for t in sorted(owned & protected_tasks):
            refusals.append('Seed task is also protected: ' + t)
        for t in sorted(tasks & partial):
            refusals.append('Task is listed as both removed and partial: ' + t)
        existing_tasks = {r['id'] for r in db.execute('SELECT id FROM tasks')}
        for t in sorted(owned - existing_tasks):
            refusals.append('Seed task not found: ' + t)
        commits = [dict(r) for r in db.execute('SELECT * FROM commits')]
        by_doc = {}
        for row in commits:
            by_doc.setdefault(row['document'], []).append(row)
        host = {r['id'] for r in db.execute('SELECT id FROM host_commits')}
        docs = set(extra_docs)
        for key, rows in by_doc.items():
            owners = {r['task'] for r in rows}
            if owners <= owned:
                docs.add(key)
            elif owners & owned and key not in extra_docs:
                pass                                             # written by a kept task too: shared, so it stays (reported below)
        for row in db.execute('SELECT * FROM messages'):
            if row['id'] in messages and row['resource']:
                docs.add(row['resource'])
        for key in sorted(extra_docs & protected_docs):
            refusals.append('Document is protected but listed for removal: ' + key)
        docs -= (protected_docs - extra_docs)                    # protected documents are simply kept, never selected by closure
        known_docs = {r['id'] for r in db.execute('SELECT id FROM documents')}
        for key in sorted(extra_docs - known_docs):
            refusals.append('Seed document not found: ' + key)
        docs &= known_docs
        for row in commits:
            if row['task'] in tasks and row['document'] not in docs:
                refusals.append('Removed task %s wrote kept document %s: make it a partial task' % (row['task'], row['document']))
        selected = {t: [] for t in TABLE_ORDER}
        selected['tasks'] = select(db, 'tasks', 'id', tasks)
        selected['messages'] = select(db, 'messages', 'id', messages)
        selected['documents'] = select(db, 'documents', 'id', docs)
        selected['commits'] = select(db, 'commits', 'document', docs)
        selected['host_commits'] = select(db, 'host_commits', 'task', tasks) + select(db, 'host_commits', 'document', docs)
        selected['executions'] = select(db, 'executions', 'task', owned)
        selected['skill_uses'] = select(db, 'skill_uses', 'task', owned)
        selected['task_reads'] = select(db, 'task_reads', 'task', owned)               # a kept task's read of a removed document stays (dangling, not edited)
        selected['citation_batches'] = select(db, 'citation_batches', 'task', owned)
        selected['task_links'] = select(db, 'task_links', 'child', tasks) + select(db, 'task_links', 'parent', tasks)
        selected['task_requests'] = select(db, 'task_requests', 'owner_task', tasks) + select(db, 'task_requests', 'child', tasks)
        selected['handoffs'] = select(db, 'handoffs', 'task', tasks)
        selected['processing'] = select(db, 'processing', 'task', tasks) + select(db, 'processing', 'message', messages)
        selected['deliveries'] = select(db, 'deliveries', 'task', tasks) + select(db, 'deliveries', 'message', messages)
        for table in ('processing', 'deliveries'):
            for r in selected[table]:
                if r['task'] and r['task'] not in owned:
                    refusals.append('Removed message %s was handled by kept task %s (%s)' % (r['message'], r['task'], table))
        outbox = select(db, 'outbox', 'task', tasks) + select(db, 'outbox', 'id', outbox_extra)
        for r in select(db, 'outbox', 'payload_document', docs):
            if r['task'] not in tasks and r['id'] not in outbox_extra:
                refusals.append('Kept outbox row %s points at a removed document' % r['id'])
        selected['outbox'] = outbox
        ids = {r['id'] for r in outbox}
        for r in outbox:
            if r['id'] in protected_unknown or r['state'] == 'unknown':
                refusals.append('Unknown-delivery outbox row would be removed: ' + r['id'])
        selected['delivery_retries'] = select(db, 'delivery_retries', 'outbox', ids)
        selected['message_actions'] = select(db, 'message_actions', 'outbox', ids)
        selected['unknown_notices'] = select(db, 'unknown_notices', 'outbox', ids)
        selected['file_replies'] = select(db, 'file_replies', 'reply_document', docs) + select(db, 'file_replies', 'report_document', docs)
        selected['report_artifacts'] = select(db, 'report_artifacts', 'document', docs)
        selected['materials'] = select(db, 'materials', 'document', docs)
        selected['wechat_imports'] = select(db, 'wechat_imports', 'document', docs)
        selected['wechat_captures'] = select(db, 'wechat_captures', 'task', owned) + select(db, 'wechat_captures', 'document', docs)
        selected['wechat_requests'] = select(db, 'wechat_requests', 'id', seed.get('wechat_requests', []))
        selected['conflicts'] = select(db, 'conflicts', 'document', docs) + select(db, 'conflicts', 'task', owned)
        selected['leases'] = select(db, 'leases', 'document', docs)
        selected['cloud_publications'] = select(db, 'cloud_publications', 'id', seed.get('cloud_publications', [])) + select(db, 'cloud_publications', 'task', owned)
        selected['cloud_copies'] = select(db, 'cloud_copies', 'id', seed.get('cloud_copies', []))
        selected['web_publications'] = select(db, 'web_publications', 'id', seed.get('web_publications', []))
        selected['web_requests'] = select(db, 'web_requests', 'id', seed.get('web_requests', [])) + select(db, 'web_requests', 'task', owned)
        # keep each row once
        for table, rows in selected.items():
            _, keys = table_columns(db, table)
            seen, unique = set(), []
            for row in rows:
                marker = tuple(row[k] for k in keys) if keys else canonical(row)
                if marker not in seen:
                    seen.add(marker)
                    unique.append(row)
            selected[table] = unique
        # a kept task whose parent/child link or request points into the removal would be left dangling
        removed_tasks = tasks
        for link in db.execute('SELECT * FROM task_links'):
            if (link['child'] in removed_tasks) != (link['parent'] in removed_tasks):
                refusals.append('Task link crosses kept/removed tasks: %s -> %s' % (link['parent'], link['child']))
        # kept documents that mention removed objects: reported, refused unless the seed allows that document
        needles = {x for x in (docs | owned | messages) if len(x) >= 12}
        for row in db.execute('SELECT * FROM documents'):
            if row['id'] in docs or not row['revision']:
                continue
            try:
                text = c._path(db, row).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            hit = sorted(n for n in needles if n in text)
            if hit:
                references.append({'document': row['id'], 'mentions': hit[:20], 'count': len(hit)})
                if row['id'] not in allowed_references:
                    refusals.append('Kept document %s mentions removed objects (%d); allow it explicitly or keep the object' % (row['id'], len(hit)))
        # ---- files: one boundary, exact lists, no links, never anything a kept row still points at ----
        roots = [Path(os.path.abspath(r)) for r in output_roots] + [base]
        for scope in c.db.get_policy(db)['scopes'].values():
            if scope.get('root'):
                roots.append(Path(os.path.abspath(scope['root'])))
        candidates = []                                                   # (path, is_directory_tree)
        for row in selected['documents']:
            candidates.extend((p, False) for p in doc_files(c, db, row))
        for row in selected['report_artifacts']:
            if row['path']:
                candidates.append((base / row['path'], False))      # stored relative to the control root
        for row in selected['conflicts']:
            if row['intent_path'] and (base / row['intent_path']).exists():
                candidates.append((base / row['intent_path'], False))
        out_roots = [Path(r).resolve() for r in output_roots]
        for row in selected['executions']:
            for key in ('output_dir', 'job_path'):
                value = row[key]
                if not value:
                    continue
                target = Path(value)
                resolved = target.resolve()
                if not any(r in resolved.parents for r in out_roots):    # strictly inside an allowed output root, never the root itself
                    refusals.append('Execution path is not strictly inside an allowed output root: ' + value)
                    continue
                if target.exists():
                    candidates.append((target, target.is_dir()))
        kept = []
        for row in db.execute('SELECT * FROM documents'):
            if row['id'] not in docs:
                path = c._path(db, row)
                kept.extend([path, path.parent / '.history' / path.name])
        for row in db.execute('SELECT * FROM report_artifacts'):
            if row['document'] not in docs and row['path']:
                kept.append(base / row['path'])
        for row in db.execute('SELECT * FROM conflicts'):
            if row['id'] not in {r['id'] for r in selected['conflicts']} and row['intent_path']:
                kept.append(base / row['intent_path'])
        for row in db.execute('SELECT * FROM executions'):
            if row['task'] not in owned:
                kept.extend(Path(row[k]) for k in ('output_dir', 'job_path') if row[k])
        kept = [k.resolve() for k in kept]
        files, dirs = {}, []
        for path, tree in candidates:
            original = Path(os.path.abspath(path))
            resolved = original.resolve()
            holder = next((r for r in roots if r == original or r in original.parents), None)
            if holder is None or holder == original:
                refusals.append('Path is outside the allowed roots: ' + str(path))
                continue
            link = link_in_chain(holder, original)
            if link:
                refusals.append('Linked path component refused: ' + link)
                continue
            if holder.resolve() not in resolved.parents:               # the resolved target must still be inside the root
                refusals.append('Path resolves outside its allowed root: ' + str(path))
                continue
            if any(k == resolved or k in resolved.parents or resolved in k.parents for k in kept):
                refusals.append('Path overlaps something a kept row still points at: ' + str(path))
                continue
            if tree and original.is_dir():
                dirs.append(str(resolved))
                for inner in list_tree(original, refusals):
                    files[str(inner.resolve())] = None
            elif original.is_file():
                files[str(resolved)] = None
        entries = []
        for path in sorted(files):
            data = Path(path).read_bytes()
            entries.append({'path': path, 'sha256': sha(data), 'bytes': len(data)})
    return {'refusals': sorted(set(refusals)), 'tables': selected, 'files': entries, 'dirs': sorted(set(dirs)), 'references': references,
            'counts': {t: len(r) for t, r in selected.items() if r}}


def plan_digest(p):
    body = {'tables': {t: sorted(canonical(r) for r in rows) for t, rows in p['tables'].items() if rows}, 'files': sorted((f['path'], f['sha256']) for f in p['files']), 'dirs': p['dirs']}
    return sha(json.dumps(body, sort_keys=True, ensure_ascii=False).encode())


def archive(p, destination, seed):
    """Copies every row and file the plan would remove. Refuses a plan that has refusals."""
    if p['refusals']:
        raise Refused('; '.join(p['refusals']))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'seed_sha256': sha(json.dumps(seed, sort_keys=True, ensure_ascii=False).encode()),
                'plan_sha256': plan_digest(p), 'tables': {}, 'files': []}
    for table, rows in p['tables'].items():
        if rows:
            data = ('\n'.join(canonical(r) for r in rows) + '\n').encode()
            (destination / (table + '.jsonl')).write_bytes(data)
            manifest['tables'][table] = {'rows': len(rows), 'sha256': sha(data)}
    for number, entry in enumerate(p['files']):
        source = Path(entry['path'])
        target = destination / 'files' / ('%d-%s' % (number, source.name))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha(target.read_bytes()) != entry['sha256']:
            raise Refused('File changed while archiving: ' + entry['path'])
        manifest['files'].append({'source': entry['path'], 'archive': str(target.relative_to(destination)), 'sha256': entry['sha256']})
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    return manifest


def verify_archive(p, destination):
    destination = Path(destination)
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf8'))
    if manifest['plan_sha256'] != plan_digest(p):
        raise Refused('Archive does not match the current plan; archive again')
    for table, info in manifest['tables'].items():
        if sha((destination / (table + '.jsonl')).read_bytes()) != info['sha256']:
            raise Refused('Archive table file changed: ' + table)
    for item in manifest['files']:
        if sha((destination / item['archive']).read_bytes()) != item['sha256']:
            raise Refused('Archive file changed: ' + item['archive'])
    return manifest


def tree_fingerprint(roots):
    """(relative path, size, mtime_ns) for every file under the given roots: a cheap before/after proof that a rehearsal wrote nothing."""
    result = {}
    for root in roots:
        root = Path(root)
        for path in sorted(root.rglob('*')):
            if path.is_file() and not is_link(path):
                info = path.stat()
                result[str(path)] = (info.st_size, info.st_mtime_ns)
    return sha(json.dumps(sorted(result.items()), sort_keys=True).encode()), len(result)


def chain_link(path):
    """First link/junction/reparse point anywhere on the absolute path as written, from the drive or filesystem root down."""
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.relative_to(path.anchor).parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and is_link(current):
            return str(current)
    return None


def rehearsal_control(control_class, copy_root, production_root, rehearsal_root):
    """A Control over a database COPY whose side effects cannot reach the production files.
    Control(copy) only isolates the database: its after-commit hook writes an action receipt under the scope roots named in
    the COPIED policy, i.e. the production directories. Here the hook is replaced so the complete audit receipts are kept
    under rehearsal_root instead. Nothing is disabled: every receipt is still written, just not into production.
    Every check runs BEFORE the Control is constructed, because constructing it already opens the database."""
    roots = {'copy': copy_root, 'production': production_root, 'rehearsal': rehearsal_root}
    written = {k: Path(os.path.abspath(v)) for k, v in roots.items()}
    for name, path in written.items():
        link = chain_link(path)
        if link:
            raise Refused('The %s root or one of its ancestors is a link/junction: %s' % (name, link))
    real = {k: v.resolve() for k, v in written.items()}
    def related(a, b):
        return a == b or a in b.parents or b in a.parents
    if related(real['copy'], real['production']):
        raise Refused('The rehearsal copy must be separate from the production control root (resolved paths overlap)')
    if related(real['rehearsal'], real['production']):
        raise Refused('The rehearsal write root must be outside the production control root (resolved paths overlap)')
    copy_db, production_db = real['copy'] / 'runtime.sqlite', real['production'] / 'runtime.sqlite'
    if copy_db.exists() and production_db.exists() and os.path.samefile(copy_db, production_db):
        raise Refused('The copy database is the production database (same file, link or hard link)')
    c = control_class(written['copy'])
    if os.path.samefile(c.db.path, production_db) if production_db.exists() else False:
        raise Refused('The copy database is the production database')
    rehearsal_real = real['rehearsal']

    def persist(rows):
        for row in rows:
            path = rehearsal_real / 'actions' / str(row['task']) / (str(row['id']) + '.json')
            link = chain_link(path)
            if link:
                raise Refused('Rehearsal receipt path crosses a link: ' + link)
            target = Path(os.path.abspath(path)).resolve()
            if rehearsal_real not in target.parents or real['production'] in target.parents:
                raise Refused('Rehearsal receipt would leave the rehearsal write root: ' + str(path))
            data = json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != data:
                raise Refused('Rehearsal action receipt changed: ' + str(path))
            path.write_bytes(data)

    c.db.after_commit = persist
    c.rehearsal_root = written['rehearsal']
    c.rehearsal_production_root = written['production']
    return c


def controller_listening(port=9764):
    with socket.socket() as probe:
        probe.settimeout(.5)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def apply(c, seed, archive_dir, output_roots=(), listening=controller_listening, extra_delete=None, remove_files=True, control_root=None, rehearsal=False):
    """Delete inside one transaction, verify nothing else changed, then remove files. `extra_delete` is a test hook."""
    if rehearsal:
        # a copy only: the production port guard does not apply, but everything that makes it safe must hold
        production = getattr(c, 'rehearsal_production_root', None)
        if production is None or getattr(c, 'rehearsal_root', None) is None:
            raise Refused('A rehearsal requires a Control made by rehearsal_control()')
        if remove_files:
            raise Refused('A rehearsal never removes files')
        if control_root is None or Path(os.path.abspath(control_root)) != production or Path(os.path.abspath(c.root)) == production:
            raise Refused('A rehearsal must run on a copy and name the production control root only for path resolution')
    elif listening():
        raise Refused('Controller port is listening; stop the controller first')
    p = plan(c, seed, output_roots, control_root)
    if p['refusals']:
        raise Refused('; '.join(p['refusals']))
    manifest = verify_archive(p, archive_dir)
    with c.lock, c.db.transaction() as db:
        before = digest_tables(db)
        for table in TABLE_ORDER:
            _, keys = table_columns(db, table)
            for row in p['tables'].get(table, []):
                db.execute('DELETE FROM %s WHERE %s' % (table, ' AND '.join('%s=?' % k for k in keys)), [row[k] for k in keys])
        if extra_delete:
            extra_delete(db)
        c.audit(db, 'administrator', None, 'test_data.purged',
                {'seed_sha256': manifest['seed_sha256'], 'plan_sha256': manifest['plan_sha256'], 'archive': str(archive_dir),
                 'counts': p['counts']})
        after = digest_tables(db)
        for table, rows in before.items():
            _, keys = table_columns(db, table)
            gone = {tuple(r[k] for k in keys) for r in p['tables'].get(table, [])} if keys else set()
            expected = {k: v for k, v in rows.items() if k not in gone}
            if table == 'audit':
                new = {k: v for k, v in after[table].items() if k not in expected}
                if len(new) != 1 or {k: v for k, v in after[table].items() if k in expected} != expected:
                    raise Refused('Audit table changed beyond the single tombstone')
                continue
            if after[table] != expected:
                raise Refused('Table changed beyond the selected rows: ' + table)
    removed, failed = [], []
    for entry in (p['files'] if remove_files else []):   # a rehearsal on a database copy must never touch the real files
        target = Path(entry['path'])
        try:
            if not target.is_file() or sha(target.read_bytes()) != entry['sha256']:
                failed.append({'path': entry['path'], 'error': 'changed or missing since the archived plan; left in place'})
                continue
            target.unlink()
            removed.append(entry['path'])
        except OSError as error:
            failed.append({'path': entry['path'], 'error': repr(error)})
    for directory in sorted((Path(d) for d in (p['dirs'] if remove_files else [])), key=lambda x: len(x.parts), reverse=True):
        for sub in sorted((x for x in directory.rglob('*') if x.is_dir() and not is_link(x)), key=lambda x: len(x.parts), reverse=True):
            try:
                sub.rmdir()                               # only empty directories: anything new stays
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
    return {'counts': p['counts'], 'files_removed': len(removed), 'files_failed': failed, 'dirs_left': [d for d in p['dirs'] if Path(d).exists()]}


def compact(db_path):
    """Run with the controller stopped, after apply: return freed pages so removed content is not left in the file."""
    import sqlite3
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute('VACUUM')
        return connection.execute('PRAGMA quick_check').fetchone()[0]
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['plan', 'archive', 'apply'])
    parser.add_argument('--framework', type=Path, required=True, metavar='<framework-root>', help='explicit framework directory for this deployment')
    parser.add_argument('--control', type=Path, required=True, metavar='<control-root>', help='explicit control directory for this deployment')
    parser.add_argument('--seed', type=Path, required=True)
    parser.add_argument('--archive-dir', type=Path)
    parser.add_argument('--output-root', action='append', default=[])
    parser.add_argument('--report', type=Path)
    parser.add_argument('--production-control-root', type=Path, help='the real control root when --control is a database copy (relative artifact paths and conflict intents resolve here)')
    parser.add_argument('--rehearsal', action='store_true', help='apply to a database COPY: rows only, never remove files or compact; needs --rehearsal-dir')
    parser.add_argument('--rehearsal-dir', type=Path, help='where a rehearsal keeps its complete audit receipts (outside production)')
    args = parser.parse_args()
    sys.path.insert(0, str(args.framework))
    from anc.control import Control
    seed = json.loads(args.seed.read_text(encoding='utf8'))
    if args.rehearsal:
        if not (args.production_control_root and args.rehearsal_dir):
            raise Refused('--rehearsal needs --production-control-root and --rehearsal-dir')
        c = rehearsal_control(Control, args.control, args.production_control_root, args.rehearsal_dir)
    else:
        c = Control(args.control)
    if args.mode == 'apply':
        result = apply(c, seed, args.archive_dir, args.output_root, remove_files=not args.rehearsal, control_root=args.production_control_root, rehearsal=args.rehearsal)
        result['compact'] = None if args.rehearsal else compact(args.control / 'runtime.sqlite')
    else:
        p = plan(c, seed, args.output_root, args.production_control_root)
        result = {'refusals': p['refusals'], 'counts': p['counts'], 'files': len(p['files']), 'references': p['references'],
                  'plan_sha256': plan_digest(p)}
        if args.mode == 'archive':
            result['manifest'] = archive(p, args.archive_dir, seed)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding='utf8')
    print(text)


if __name__ == '__main__':
    main()
