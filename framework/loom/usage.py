"""Per-task token usage and the account quota snapshot, recorded when an execution finishes (panel numbers, no model calls).

Only numbers are stored: token counts from the CLI's turn.completed events, the model/effort the job was launched with, and the weekly-quota
object that Codex writes into its own session file.  Nothing here reads message text, and a failure here must never touch the reply or the task."""
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone

LIMIT_KEYS = ('rate_limits',)
TAIL_BYTES = 400_000
WINDOWS = ('primary', 'secondary')
WEEK_MINUTES = 10080
SESSION_FILES = 3          # newest session files looked at
SESSION_BUDGET = 2000      # directory entries visited at most, however many session files have piled up


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_events(path):
    """Token totals (sum over turn.completed usage), count of completed tool calls (de-duplicated by item id)."""
    totals = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0}
    tools, seen = 0, set()
    path = Path(path)
    if not path.is_file():
        return None
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get('usage')
            if event.get('type') == 'turn.completed' and isinstance(usage, dict):
                for key in totals:
                    if _number(usage.get(key)):
                        totals[key] += usage[key]
            item = event.get('item')
            if event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') in ('mcp_tool_call', 'tool_call'):
                if item.get('id') not in seen:
                    seen.add(item.get('id'))
                    tools += 1
    totals['tool_calls'] = tools
    return totals


def job_model(job_path):
    """(model, effort) from the launch argv; (None, None) if not found."""
    try:
        argv = json.loads(Path(job_path).read_text(encoding='utf-8-sig')).get('argv') or []
    except (OSError, ValueError):
        return None, None
    model = effort = None
    for index, arg in enumerate(argv):
        if arg == '-m' and index + 1 < len(argv):
            model = argv[index + 1]
        if isinstance(arg, str) and arg.startswith('model_reasoning_effort='):
            try:
                effort = json.loads(arg.split('=', 1)[1])
            except ValueError:
                effort = arg.split('=', 1)[1].strip('"')
    return model, effort


def _find_limits(node, depth=0):
    if depth > 8:
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            if key in LIMIT_KEYS and isinstance(value, dict):
                if any(isinstance(value.get(name), dict) and _number(value[name].get('used_percent')) for name in WINDOWS):
                    return value
        for value in node.values():
            found = _find_limits(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:50]:
            found = _find_limits(value, depth + 1)
            if found:
                return found
    return None


def recent_session_files(base, want=SESSION_FILES, budget=SESSION_BUDGET):
    """The newest `want` session files without walking the whole history: directories and file names sort by time (year/month/day,
    rollout-<timestamp>-...), so go newest-first and stop once enough are found.  `budget` is a HARD limit on directory entries read, counted while
    iterating: a folder that would exceed it is abandoned and nothing is returned at all (no quota this time) rather than guessing or stalling."""
    found, visited = [], [0]

    def listing(folder):
        names = []
        try:
            with os.scandir(folder) as scan:
                for entry in scan:
                    visited[0] += 1
                    if visited[0] > budget:
                        return None
                    names.append((entry.name, entry.is_dir(follow_symlinks=False), entry.is_file(follow_symlinks=False)))
        except OSError:
            return []
        return sorted(names, reverse=True)

    def walk(folder):
        """False when the budget ran out."""
        if len(found) >= want:
            return True
        entries = listing(folder)
        if entries is None:
            return False
        for name, is_dir, _ in entries:
            if is_dir and not walk(os.path.join(folder, name)):
                return False
            if len(found) >= want:
                return True
        for name, _, is_file in entries:
            if is_file and name.endswith('.jsonl') and len(found) < want:
                found.append(Path(folder) / name)
        return True

    if not walk(str(base)):
        return []
    return found


def latest_limits(codex_home):
    """Newest rate_limits object found in the newest session files of this Codex home, parsed structurally (never by text match).
    Returns (limits_dict, observed_time_iso_or_None, file_mtime_iso) or None.  Reads at most SESSION_FILES files, TAIL_BYTES each."""
    sidecar = Path(codex_home) / 'loom-rate-limits.json'
    host_snapshot = None
    try:
        value = json.loads(sidecar.read_text(encoding='utf-8'))
        limits = _find_limits(value)
        if limits and isinstance(value.get('timestamp'), str):
            host_snapshot = limits, value['timestamp'], datetime.fromtimestamp(sidecar.stat().st_mtime, timezone.utc).isoformat()
    except (OSError, ValueError):
        pass
    base = Path(codex_home) / 'sessions'
    if not base.is_dir():
        return host_snapshot
    files = sorted(recent_session_files(base), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        size = path.stat().st_size
        with path.open('rb') as stream:
            if size > TAIL_BYTES:
                stream.seek(size - TAIL_BYTES)
            chunk = stream.read().decode('utf-8', 'replace')
        hit = observed = None
        for line in chunk.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            limits = _find_limits(event) if isinstance(event, dict) else None
            if limits:
                hit = limits
                stamp = event.get('timestamp')
                observed = stamp if isinstance(stamp, str) else None
        if hit:
            if host_snapshot and host_snapshot[2] >= datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat():
                return host_snapshot
            return hit, observed, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return host_snapshot


def record(control, task, output_dir, job_path, codex_home=None):
    """Write the usage row (idempotent) and, when the weekly quota changed, a quota snapshot.  Returns what it wrote; swallows nothing silently
    except by the caller, which wraps this in try/except and audits a failure."""
    usage = parse_events(Path(output_dir) / 'events.jsonl')
    if usage is None:
        return {'usage': False, 'quota': False}
    model, effort = job_model(job_path)
    now = datetime.now(timezone.utc).isoformat()
    wrote_quota = False
    found = latest_limits(codex_home) if codex_home else None      # disk scan BEFORE taking the control lock (it must never delay scheduling or receiving)
    with control.lock, control.db.transaction() as db:
        row = db.execute('SELECT scope,actor,kind,created FROM tasks WHERE id=?', (task,)).fetchone()
        execution = db.execute('SELECT started,finished FROM executions WHERE task=?', (task,)).fetchone()
        if not row:
            return {'usage': False, 'quota': False}
        db.execute('INSERT OR IGNORE INTO usage_log(task,scope,actor,kind,model,effort,input_tokens,cached_tokens,output_tokens,reasoning_tokens,tool_calls,started,finished,recorded) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (task, row['scope'], row['actor'], row['kind'], model, effort, int(usage['input_tokens']), int(usage['cached_input_tokens']),
                    int(usage['output_tokens']), int(usage['reasoning_output_tokens']), int(usage['tool_calls']),
                    execution['started'] if execution else None, execution['finished'] if execution else None, now))
        if found:
            limits, observed, mtime = found
            account = str(Path(codex_home).resolve()).lower()
            credits = limits.get('credits') if isinstance(limits.get('credits'), dict) else {}
            for name in WINDOWS:                                  # each window is stored under its own window_minutes; the weekly one is the 10080 row, whichever slot it came in
                window = limits.get(name)
                if not isinstance(window, dict) or not _number(window.get('used_percent')):
                    continue
                minutes = window.get('window_minutes')
                last = db.execute('SELECT used_percent,resets_at,recorded FROM quota_snapshots WHERE account=? AND window_minutes IS ? ORDER BY id DESC LIMIT 1',
                                  (account, minutes)).fetchone()
                if (not last or last['used_percent'] != window.get('used_percent') or last['resets_at'] != window.get('resets_at')
                        or (datetime.now(timezone.utc) - datetime.fromisoformat(last['recorded'])).total_seconds() > 3600):
                    db.execute('INSERT INTO quota_snapshots(account,scope,task,window_minutes,used_percent,resets_at,has_credits,observed,observed_source,recorded) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (account, row['scope'], task, minutes, window.get('used_percent'), window.get('resets_at'),
                                1 if credits.get('has_credits') else 0, observed or mtime, 'event' if observed else 'file_mtime', now))
                    wrote_quota = True
    return {'usage': True, 'quota': wrote_quota}
