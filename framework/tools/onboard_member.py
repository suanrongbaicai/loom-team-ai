"""Add one team member and their private conversation to the live policy (落地日志 0108).

Replaces hand-editing the policy: `add_member` is a pure function (old policy -> new policy) so it can be reviewed and
tested; `apply` takes the controller/gateway leases (services must be stopped), checks the expected epoch, saves the
previous policy as evidence, writes the new one through Control.set_policy (epoch +1, audit), creates the scope root and
personnel file, and deploys the same approved skill versions that an existing scope (--like) already has.
It only ADDS: an existing member, scope, binding or app is refused, never overwritten.  Group chats need no step here:
they are admitted automatically when every member is on the roster (loom/group_admission.py).
Output: names, counts, hashes - never policy values."""
import argparse
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.control import Control  # noqa: E402
from loom.instance import InstanceBusy, InstanceLock  # noqa: E402
from loom.skills import Skills  # noqa: E402


from loom.onboard_member_core import Refused, add_member  # noqa: E402,F401


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def apply(control_root, new_member_args, approval, expected_epoch, evidence_dir, like_scope, workspace,
          personnel_template, gateway_dirs=()):
    if not approval or len(approval) < 12:
        raise Refused('A written approval reference is required')
    root = Path(control_root)
    with contextlib.ExitStack() as stack:
        for lock in [root / '.controller.lock'] + [Path(g) / '.gateway.lock' for g in gateway_dirs]:
            try:
                stack.enter_context(InstanceLock(lock))
            except InstanceBusy:
                raise Refused('A service still holds ' + lock.name + '; stop services first')
        c = Control(root)
        with c.db.transaction() as db:
            body = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0]
            skills = db.execute('SELECT skill, version FROM skill_deployments WHERE scope=?', (like_scope,)).fetchall()
        state = json.loads(body)
        if state['epoch'] != expected_epoch:
            raise Refused('Policy epoch is %s, not the expected %s' % (state['epoch'], expected_epoch))
        if not skills:
            raise Refused('Reference scope has no deployed skills: ' + like_scope)
        new = add_member(state, **new_member_args)
        member = new_member_args['member']
        scope_info = new['scopes'][member + '-private']
        workspace = Path(workspace)
        if workspace.exists():
            raise Refused('Workspace already exists: ' + str(workspace))
        evidence = Path(evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True)
        saved = evidence / ('policy-before-epoch-%d.json' % state['epoch'])
        if saved.exists():
            raise Refused('Evidence file already exists: ' + str(saved))
        saved.write_text(body, encoding='utf-8')
        scope_root = Path(scope_info['root'])
        personnel = Path(scope_info['personnel_file'])
        made = []                  # everything this call creates, removed again if any step fails
        scope_name = member + '-private'
        try:
            for folder in (scope_root, workspace / '.agents' / 'skills'):
                top = folder
                while not top.parent.exists():
                    top = top.parent
                if not folder.exists():
                    made.append(top)
                    folder.mkdir(parents=True)
            if not personnel.exists():
                personnel.parent.mkdir(parents=True, exist_ok=True)
                personnel.write_text(Path(personnel_template).read_text(encoding='utf-8'), encoding='utf-8')
                made.append(personnel)
            deployer = Skills(c)
            for row in skills:                 # the policy is written LAST: until then nothing refers to the new member
                deployer.deploy(scope_name, row[0], row[1], workspace)
            epoch = c.set_policy(new, approval)
        except BaseException as error:
            problems = []
            for path in made:
                try:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                except OSError as again:
                    problems.append('%s: %r' % (path, again))
            try:
                with c.db.transaction() as db:
                    db.execute('DELETE FROM skill_deployments WHERE scope=?', (scope_name,))
            except Exception as again:
                problems.append('skill_deployments: %r' % (again,))
            raise Refused('Onboarding failed (%r) and was undone%s' % (
                error, '' if not problems else '; COULD NOT undo everything, inspect: ' + '; '.join(problems)))
    return {'member': member, 'scope': member + '-private', 'epoch_before': expected_epoch, 'epoch_after': epoch,
            'skills_deployed': len(skills), 'previous_policy_sha256': sha(body), 'saved_previous_policy': str(saved)}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--control', required=True)
    p.add_argument('--member', required=True)
    p.add_argument('--platform', default='feishu')
    p.add_argument('--tenant', required=True)
    p.add_argument('--app', required=True)
    p.add_argument('--account', required=True, help='the member\'s open_id in that application')
    p.add_argument('--union', help='the member\'s tenant-wide id, if known')
    p.add_argument('--evidence-text', required=True, help='how the identity was verified')
    p.add_argument('--chat', required=True)
    p.add_argument('--root', required=True)
    p.add_argument('--personnel-file', required=True)
    p.add_argument('--workspace', required=True)
    p.add_argument('--like', required=True, help='existing scope whose approved skills are deployed to the new one')
    p.add_argument('--personnel-template', required=True)
    p.add_argument('--approval', required=True)
    p.add_argument('--expected-epoch', type=int, required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--gateway-dir', action='append', default=[])
    a = p.parse_args(argv)
    try:
        result = apply(a.control, dict(member=a.member, platform=a.platform, tenant=a.tenant, app=a.app, account=a.account,
                                       evidence=a.evidence_text, chat=a.chat, root=a.root, personnel_file=a.personnel_file,
                                       union=a.union), a.approval, a.expected_epoch, a.evidence, a.like, a.workspace,
                       a.personnel_template, a.gateway_dir)
    except Refused as error:
        print(json.dumps({'refused': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
