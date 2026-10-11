"""Authenticated transport identity, including background task ancestry."""

def bot(db, task, state=None):
    seen = set()
    for _ in range(64):
        if task in seen:
            return None
        seen.add(task)
        apps = {r[0] for r in db.execute('SELECT app FROM deliveries WHERE task=? UNION '
            'SELECT app FROM processing WHERE task=?', (task, task))}
        if len(apps) == 1:
            return apps.pop()
        if apps:
            return None  # Ambiguous delivery never selects an account.
        parent = db.execute('SELECT parent FROM task_links WHERE child=?', (task,)).fetchone()
        if not parent:
            if state:
                row = db.execute('SELECT scope FROM tasks WHERE id=?', (task,)).fetchone()
                scope = state['scopes'].get(row[0], {}) if row else {}
                configured = scope.get('transport', {}).get('apps', [])
                if len(configured) == 1:
                    return configured[0]
            return None
        task = parent[0]
    return None


def key(db, task, state):
    row = db.execute('SELECT scope FROM tasks WHERE id=?', (task,)).fetchone()
    if not row:
        return None
    scope = state['scopes'].get(row[0], {})
    transport = scope.get('transport', {})
    app = bot(db, task, state)
    if not app or not transport.get('chat') or not transport.get('tenant'):
        return None
    return app, transport['tenant'], transport['chat']


def same_bot(db, source, target, state):
    first, second = bot(db, source, state), bot(db, target, state)
    if first is None or second is None:
        for task in (source, target):
            row = db.execute('SELECT scope FROM tasks WHERE id=?', (task,)).fetchone()
            scope = state['scopes'].get(row[0], {}) if row else {}
            if len(scope.get('transport', {}).get('apps', [])) > 1:
                return False
    return first == second
