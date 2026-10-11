"""Facts and actions behind the work panel (落地日志 0122).  Pure data: no windows, no network, testable anywhere.

Reads are read-only SQLite queries.  Changes go through the same trusted paths the rest of the system uses (policy through
Control.set_policy, models through member_settings), are audited, and refuse to run while tasks are being worked on if they
would cut those tasks off (a policy change raises the global authorisation epoch)."""
import json
import statistics
from datetime import datetime, timedelta, timezone

from .member_labels import label_of
from .member_settings import DEFAULT_ALLOWED, all_rows


class Busy(RuntimeError):
    pass


def _parse(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def reply_seconds(db, since, limit=20):
    """Seconds from a task being created to its first reply being sent, for recent finished turns (what a person feels)."""
    rows = db.execute("SELECT id,actor,created FROM tasks WHERE kind='turn' AND state='done' AND created>=? ORDER BY created DESC LIMIT ?",
                      (since, limit * 3)).fetchall()
    out = []
    for task, actor, created in rows:
        sent = db.execute("SELECT MIN(time) FROM audit WHERE task=? AND action='reply.sent'", (task,)).fetchone()[0]
        a, b = _parse(created), _parse(sent)
        if a and b and b >= a:
            out.append((actor, (b - a).total_seconds()))
        if len(out) >= limit:
            break
    return out


def _median(values):
    return round(statistics.median(values), 1) if values else None


def overview(db, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat()
    times = [s for _, s in reply_seconds(db, since)]
    failed = db.execute("SELECT COUNT(*) FROM tasks WHERE state='needs_attention' AND created>=?", (since,)).fetchone()[0]
    running = db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('queued','preparing','running','ingesting')").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM tasks WHERE state='done' AND kind='turn' AND created>=?", (since,)).fetchone()[0]
    return {'running_or_waiting': running, 'done_24h': done, 'failed_24h': failed,
            'median_reply_seconds': _median(times), 'slowest_reply_seconds': round(max(times), 1) if times else None,
            'samples': len(times)}


def worker_view(member, state, config):
    """Which account and model this person's tasks run on, from the configuration and the person's own override."""
    scopes = (config or {}).get('scopes', {})
    own = next((name for name, s in state['scopes'].items()
                if s.get('kind') == 'private' and s.get('participants') == [member] and name in scopes), None)
    shared = (config or {}).get('shared_worker_scope')
    base = dict(scopes.get(own, {})) if own else (dict(scopes.get(shared, {}), **(config or {}).get('shared_worker_overrides', {}))
                                                  if shared in scopes else {})
    return {'account': 'own' if own else ('shared' if shared in scopes else 'none'),
            'model': base.get('model') or 'example-model-b', 'reasoning': base.get('reasoning') or 'high',
            'model_is_default': not base.get('model')}


def members(db, state, config, now=None):
    now = now or datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).isoformat()
    settings = all_rows(db)
    times = {}
    for actor, seconds in reply_seconds(db, day, limit=200):
        times.setdefault(actor, []).append(seconds)
    rows = []
    for member, info in sorted(state['members'].items()):
        label = label_of(db, member)
        view = worker_view(member, state, config)
        mine = settings.get(member, {})
        last = db.execute('SELECT MAX(time) FROM audit WHERE actor=?', (member,)).fetchone()[0]
        today = db.execute("SELECT COUNT(*) FROM tasks WHERE actor=? AND kind='turn' AND created>=?", (member, day)).fetchone()[0]
        rows.append({
            'id': member, 'name': label.get('call_as') or member, 'about': label.get('about'),
            'status': 'inactive' if not info.get('active', True) else 'team',
            'roles': info.get('roles', []), 'account': view['account'],
            'model': mine.get('model') or view['model'], 'reasoning': mine.get('reasoning') or view['reasoning'],
            'model_overridden': bool(mine.get('model') or mine.get('reasoning')), 'default_model': view['model'],
            'default_reasoning': view['reasoning'], 'last_active': last, 'tasks_24h': today,
            'median_reply_seconds': _median(times.get(member, []))})
    return rows


def _busy(db):
    return bool(db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','preparing','running','ingesting') LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM outbox WHERE state IN ('queued','sending') LIMIT 1").fetchone())


def change_member(c, member, action, role=None, actor='panel'):
    """Roster changes made from the panel.  Waits for an idle system (the epoch bump would cut off running tasks)."""
    if action not in ('make_team_member', 'deactivate', 'reactivate'):
        raise ValueError('未知操作')
    with c.lock:
        with c.db.transaction() as db:
            state = c.db.get_policy(db)
            if member not in state['members']:
                raise ValueError('没有这个成员')
            if _busy(db):
                raise Busy('机器人正在处理别人的任务，请一两分钟后再试（这样不会打断他们）。')
        entry = state['members'][member]
        if action == 'make_team_member':
            entry['team_member'] = True
            # auto_basic is kept: it only marks an automatically registered member (later chats/apps may extend registration), it is not a lower tier
            if role:
                entry['roles'] = sorted(set(entry.get('roles', [])) | {str(role)[:40]})
        elif action == 'deactivate':
            entry['active'] = False
        else:
            entry['active'] = True
        c.set_policy(state, 'work panel: %s %s' % (action, member))
        return {'ok': True}


def allowed_models(config):
    return tuple((config or {}).get('allowed_models') or DEFAULT_ALLOWED)
