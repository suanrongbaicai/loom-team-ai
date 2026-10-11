"""Remove named top-level approval sections from the live policy (落地日志 0101).

Used once to drop the engineering-test WeChat authorisations (`wechat_sources`, `wechat_captures`, `wechat_accounts`)
that the wipe kept verbatim.  Administrator-only: it takes the controller and gateway leases (so no service can run),
checks the expected epoch, saves the previous policy text beside the evidence, and writes the new policy through the
same `Control.set_policy` entry a deployment uses (epoch +1, audit `policy.changed`, queued sends need reauthorisation).
Only the allow-listed section names can be emptied; members, apps, scopes and grants are never touched here.
Output: names, counts, hashes - never policy values."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anc.control import Control  # noqa: E402
from anc.instance import InstanceBusy, InstanceLock  # noqa: E402

REMOVABLE = ('wechat_sources', 'wechat_captures', 'wechat_accounts')


class Refused(RuntimeError):
    pass


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def edit(control_root, sections, approval, expected_epoch, evidence_dir, gateway_dirs=()):
    unknown = [s for s in sections if s not in REMOVABLE]
    if unknown or not sections:
        raise Refused('Only these sections may be emptied: ' + ', '.join(REMOVABLE))
    if not approval or len(approval) < 12:
        raise Refused('A written approval reference is required')
    root = Path(control_root)
    locks = [root / '.controller.lock'] + [Path(g) / '.gateway.lock' for g in gateway_dirs]
    with contextlib.ExitStack() as stack:
        for lock in locks:
            try:
                stack.enter_context(InstanceLock(lock))
            except InstanceBusy:
                raise Refused('A service still holds ' + lock.name + '; stop services first')
        c = Control(root)
        with c.db.transaction() as db:
            body = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0]
        state = json.loads(body)
        if state['epoch'] != expected_epoch:
            raise Refused('Policy epoch is %s, not the expected %s' % (state['epoch'], expected_epoch))
        evidence = Path(evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True)
        saved = evidence / ('policy-before-epoch-%d.json' % state['epoch'])
        if saved.exists():
            raise Refused('Evidence file already exists: ' + str(saved))
        saved.write_text(body, encoding='utf-8')
        removed = {}
        for name in sections:
            removed[name] = len(state.get(name, {}))
            state[name] = {}
        epoch = c.set_policy(state, approval)
        with c.db.transaction() as db:
            after = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0]
    result = json.loads(after)
    others_same = all(json.dumps(result[k], sort_keys=True) == json.dumps(json.loads(body)[k], sort_keys=True)
                      for k in result if k not in sections and k not in ('epoch', 'roster_version'))
    return {'epoch_before': expected_epoch, 'epoch_after': epoch, 'removed_entry_counts': removed,
            'previous_policy_sha256': sha(body), 'new_policy_sha256': sha(after), 'other_sections_identical': others_same,
            'saved_previous_policy': str(saved)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', required=True)
    parser.add_argument('--section', action='append', required=True)
    parser.add_argument('--approval', required=True)
    parser.add_argument('--expected-epoch', type=int, required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--gateway-dir', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        result = edit(args.control, args.section, args.approval, args.expected_epoch, args.evidence, args.gateway_dir)
    except Refused as error:
        print(json.dumps({'refused': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result['other_sections_identical'] else 1


if __name__ == '__main__':
    sys.exit(main())
