"""Administrator tool: switch the team to "open" (explicit administrator opt-in: verified members share readable records, including private-chat records).
Sets the single top-level policy flag `team_open` through Control.set_policy (epoch +1, audit policy.changed, queued sends need reauthorisation) and nothing else.
Like the panel's member action it waits for an idle system instead of stopping services: it refuses (Busy) when any task or send is in flight.
Reversible: run with --off.  Output: counts and epochs only.
<python> policy_open.py --control <control folder> --approval "<written reference>" [--off] [--apply]    (without --apply it only reports)"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.control import Control  # noqa: E402


class Busy(RuntimeError):
    pass


def busy(db):
    return bool(db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','preparing','running','ingesting') LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM outbox WHERE state IN ('queued','sending') LIMIT 1").fetchone())


def switch(c, approval, on=True, apply=False):
    with c.lock:
        with c.db.transaction() as db:
            state = c.db.get_policy(db)
            if apply and busy(db):
                raise Busy('The bot is handling a task or a send; try again when idle')
        before = {'epoch': state['epoch'], 'team_open': bool(state.get('team_open')),
                  'members': len(state['members']), 'scopes': len(state['scopes'])}
        if not apply:
            return {'would_set': on, 'before': before}
        if on:
            state['team_open'] = True
            state['team_open_approval'] = approval[:200]
        else:
            state.pop('team_open', None)
            state.pop('team_open_approval', None)
        epoch = c.set_policy(state, approval)
    with c.db.transaction() as db:
        after = c.db.get_policy(db)
    return {'before': before, 'after': {'epoch': epoch, 'team_open': bool(after.get('team_open')), 'members': len(after['members']), 'scopes': len(after['scopes'])}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--approval', required=True)
    parser.add_argument('--off', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if len(args.approval) < 12:
        print(json.dumps({'refused': 'a written approval reference is required'}))
        return 2
    try:
        print(json.dumps(switch(Control(args.control), args.approval, not args.off, args.apply), ensure_ascii=False, indent=1))
    except Busy as error:
        print(json.dumps({'busy': str(error)}))
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
