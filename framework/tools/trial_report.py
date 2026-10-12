"""Read-only trial report: where response time goes, and whether the knowledge features are being used.

Opens runtime.sqlite read-only and never writes. Times come from the protected records:
  wait         task created (message accepted)   -> model execution started
  model        model execution started            -> finished
  first_reply  task created                       -> first reply.sent receipt (a progress note counts)
  final_reply  task created                       -> last reply.sent receipt
Tool use per turn is counted from the task's audit rows, which shows how many record/search round trips a turn makes.

    python tools/trial_report.py --control C [--days 7] [--json out.json]
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

TOOL_BUCKETS = ((0, 2), (3, 5), (6, 10), (11, 20), (21, None))
BOOKKEEPING = {'task.created', 'message.received', 'rules.delivered', 'rules.checked', 'reply.sent', 'reply.unknown',
               'skill.loaded', 'skill.adopted'}


def seconds(start, end):
    if not start or not end:
        return None
    value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return value if value >= 0 else None


def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'count': 0}
    pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]
    return {'count': len(values), 'p50': round(pick(0.5), 1), 'p90': round(pick(0.9), 1),
            'min': round(values[0], 1), 'max': round(values[-1], 1)}


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def report(database, days=7, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    uri = Path(database).absolute().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        present = tables(db)
        turns = [dict(r) for r in db.execute(
            "SELECT t.id,t.created,t.kind,t.state,e.started,e.finished FROM tasks t LEFT JOIN executions e ON e.task=t.id "
            "WHERE t.kind='turn' AND t.created>=? ORDER BY t.created", (since,))]
        sent = {}
        for row in db.execute("SELECT task,MIN(time) first,MAX(time) last FROM audit WHERE action='reply.sent' AND time>=? GROUP BY task", (since,)):
            sent[row['task']] = (row['first'], row['last'])
        usage = {}
        if 'usage_log' in present:
            usage = {r['task']: dict(r) for r in db.execute('SELECT * FROM usage_log WHERE recorded>=?', (since,))}
        actions = {}
        for row in db.execute('SELECT task,action,COUNT(*) n FROM audit WHERE task IS NOT NULL AND time>=? GROUP BY task,action', (since,)):
            if row['action'] not in BOOKKEEPING:
                actions.setdefault(row['task'], {})[row['action']] = row['n']
        features = {
            'member_preferences': db.execute('SELECT COUNT(*) FROM member_preferences').fetchone()[0] if 'member_preferences' in present else None,
            'share_requests': ({r[0]: r[1] for r in db.execute('SELECT state,COUNT(*) FROM share_requests WHERE created>=? GROUP BY state', (since,))}
                               if 'share_requests' in present else None),
            'proposals_by_category': {(r[0] or '(none)'): r[1] for r in db.execute(
                "SELECT json_extract(body,'$.category'),COUNT(*) FROM proposals GROUP BY 1")},
            'skill_versions_used': {r[0] + '@' + r[1]: r[2] for r in db.execute(
                'SELECT u.skill,u.version,COUNT(*) FROM skill_uses u JOIN tasks t ON t.id=u.task WHERE t.created>=? GROUP BY 1,2', (since,))},
        }
    rows = []
    for turn in turns:
        first, last = sent.get(turn['id'], (None, None))
        use = usage.get(turn['id'], {})
        rows.append({'wait': seconds(turn['created'], turn['started']), 'model': seconds(turn['started'], turn['finished']),
                     'first_reply': seconds(turn['created'], first), 'final_reply': seconds(turn['created'], last),
                     'tool_calls': use.get('tool_calls'), 'input_tokens': use.get('input_tokens'),
                     'cached_tokens': use.get('cached_tokens'), 'model_name': use.get('model'), 'effort': use.get('effort'),
                     'actions': sum(actions.get(turn['id'], {}).values())})
    by_tools = []
    for low, high in TOOL_BUCKETS:
        group = [r for r in rows if r['tool_calls'] is not None and r['tool_calls'] >= low and (high is None or r['tool_calls'] <= high)]
        if group:
            by_tools.append({'tool_calls': '%d-%s' % (low, high if high is not None else ''), 'final_reply': stats(r['final_reply'] for r in group),
                             'model': stats(r['model'] for r in group)})
    by_model = {}
    for r in rows:
        if r['model_name']:
            by_model.setdefault('%s/%s' % (r['model_name'], r['effort']), []).append(r['final_reply'])
    totals = {}
    for task_actions in actions.values():
        for name, n in task_actions.items():
            totals[name] = totals.get(name, 0) + n
    measured = [r for r in rows if r['input_tokens']]
    return {
        'window': {'since': since, 'days': days, 'turns': len(rows), 'answered': sum(1 for r in rows if r['final_reply'] is not None)},
        'latency_seconds': {k: stats(r[k] for r in rows) for k in ('wait', 'model', 'first_reply', 'final_reply')},
        'by_tool_calls': by_tools,
        'by_model_and_effort': {k: stats(v) for k, v in sorted(by_model.items())},
        'tokens': {'turns_measured': len(measured),
                   'input_p50': stats(r['input_tokens'] for r in measured).get('p50'),
                   'cached_share': (round(sum(r['cached_tokens'] or 0 for r in measured) / sum(r['input_tokens'] for r in measured), 2)
                                    if measured else None)},
        'actions_per_turn': {name: round(n / len(rows), 2) for name, n in sorted(totals.items(), key=lambda kv: -kv[1])[:12]} if rows else {},
        'features': features,
        'not_measured': '复核日期是否被读到、模型是否按规则分流，数据库里没有直接记录，需按试用清单人工核对。',
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', type=Path, required=True, help='controller directory containing runtime.sqlite')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--json', type=Path)
    args = parser.parse_args(argv)
    result = report(args.control / 'runtime.sqlite', args.days)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
