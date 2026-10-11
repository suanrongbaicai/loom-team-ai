"""Version-bound proposals. Real authority comes from protected configuration."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from .policy import Denied, instant, utcnow


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


POLICY_FIELDS = {'grants', 'decision_rules', 'handoff_rules'}


class Governance:
    def __init__(self, control):
        self.c = control

    def _rule(self, state, rule, category):
        if not state.get('governance_confirmed'):
            raise Denied('Initial real roster and decision rules not jointly confirmed')
        if rule == 'unanimous':
            voters = sorted(p for p, m in state['members'].items()
                            if m.get('active') and m.get('verified') and m.get('team_member', True))
            definition = {'rule': rule, 'validity_seconds': state.get('proposal_validity_seconds')}
        else:
            definition = state.get('decision_rules', {}).get(rule)
            if not definition or not definition.get('approval') or not definition.get('active'):
                raise Denied('Decision rule unapproved')
            if category not in definition['categories']:
                raise Denied('Decision outside the approved rule scope')
            now = datetime.now(timezone.utc)
            if ((definition.get('starts_at') and instant(definition['starts_at']) > now)
                    or (definition.get('expires_at') and instant(definition['expires_at']) <= now)):
                raise Denied('Decision rule is not currently effective')
            voters = sorted(set(definition['approvers']))
        if not voters or any(not state['members'].get(p, {}).get('active')
                             or not state['members'].get(p, {}).get('verified') for p in voters):
            raise Denied('No complete verified approver set')
        return voters, definition

    def _current(self, db, item, state, version):
        if not item or item['version'] != version:
            raise Denied('Proposal version unavailable')
        row = db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (item['id'],)).fetchone()
        if not row:
            raise Denied('Proposal has no frozen approval evidence; submit a new proposal')
        guard = json.loads(row[0])
        body = json.loads(item['body'])
        if guard.get('revoked') or (guard.get('expires_at') and instant(guard['expires_at']) <= datetime.now(timezone.utc)):
            raise Denied('Proposal revoked or expired')
        voters, definition = self._rule(state, item['approval_rule'], body.get('category'))
        if ((item['approval_rule'] == 'unanimous' and item['roster_version'] != state['roster_version'])
                or voters != json.loads(item['voters'])
                or canonical(definition) != guard['rule'] or canonical(body) != guard['content']):
            raise Denied('Approval basis changed; new proposal required')
        return guard

    def propose(self, ctx, body, rule='unanimous'):
        if not isinstance(body, dict) or not body:
            raise ValueError('Concrete proposed content is required')
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, True)
            state = policy.state
            voters, definition = self._rule(state, rule, body.get('category'))
            patch = body.get('policy_patch', {})
            if not isinstance(patch, dict) or not set(patch).issubset(POLICY_FIELDS):
                raise Denied('Unsupported policy change; protected technical configuration is not a proposal')
            if patch and rule != 'unanimous' and not set(patch).issubset(definition.get('policy_fields', [])):
                raise Denied('Decision rule does not authorize these policy fields')
            duration = definition.get('validity_seconds')
            if duration is not None and (type(duration) is not int or duration <= 0):
                raise Denied('Approved proposal validity is invalid')
            expires = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat() if duration else None
            key = uuid.uuid4().hex
            db.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?)',
                       (key, 1, state['roster_version'], json.dumps(voters), json.dumps(body, ensure_ascii=False), 'pending', rule))
            guard = {'created': utcnow(), 'expires_at': expires, 'rule': canonical(definition),
                     'creator_task': ctx.task,
                     'content': canonical(body), 'scope': ctx.scope,
                     'audience': sorted(policy.recipients(ctx)), 'revoked': None,
                     'sources': [r[0] for r in db.execute('SELECT document FROM task_reads WHERE task=?', (ctx.task,))],
                     'policy_base': {key: state.get(key) for key in patch}}
            db.execute('INSERT INTO proposal_guards VALUES(?,?)', (key, json.dumps(guard)))
            self.c.audit(db, ctx.principal, ctx.task, 'proposal.created', {'id': key, 'voters': voters, 'rule': rule})
            return {'id': key, 'version': 1, 'voters': voters, 'expires_at': expires,
                    'state': 'pending', 'review_command': '/anc-proposal ' + key,
                    'vote_command': '/anc-vote ' + key + ' 1 approve'}

    def review(self, ctx, proposal):
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            item = db.execute('SELECT * FROM proposals WHERE id=?', (proposal,)).fetchone()
            row = db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (proposal,)).fetchone()
            if not item or not row:
                raise Denied('Proposal unavailable in this audience')
            guard = json.loads(row[0])
            # A voter identity is not permission to disclose private source content.
            if not policy.recipients(ctx).issubset(guard['audience']):
                raise Denied('Proposal unavailable in this audience')
            for source in guard['sources']:
                _, resource = self.c._document(db, source)
                policy.require_read(ctx, resource)
                db.execute('INSERT OR IGNORE INTO task_reads VALUES(?,?)', (ctx.task, source))
            valid = True
            try:
                self._current(db, item, policy.state, item['version'])
            except Denied:
                valid = False
            return {'id': item['id'], 'version': item['version'], 'body': json.loads(item['body']),
                    'voters': json.loads(item['voters']), 'state': item['state'], 'current': valid,
                    'applied_epoch': guard.get('applied_epoch'), 'policy_base': guard.get('policy_base', {}),
                    'approval_rule': item['approval_rule'],
                    'expires_at': guard['expires_at'],
                    'votes': {r[0]: r[1] for r in db.execute('SELECT voter,decision FROM votes WHERE proposal=?', (proposal,))}}

    def vote(self, ctx, proposal, version, decision):
        if decision not in ('approve', 'reject'):
            raise ValueError('Explicit approve or reject required')
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, True)
            item = db.execute('SELECT * FROM proposals WHERE id=?', (proposal,)).fetchone()
            if not item or item['state'] not in ('pending', 'approved', 'rejected'):
                raise Denied('Proposal is not current and pending')
            self._current(db, item, policy.state, version)
            voters = json.loads(item['voters'])
            if ctx.principal not in voters:
                raise Denied('Not a frozen approver')
            old = db.execute('SELECT decision FROM votes WHERE proposal=? AND voter=?', (proposal, ctx.principal)).fetchone()
            if old:
                if old[0] != decision:
                    raise Denied('Conflicting vote; submit a new proposal')
                return item['state']
            if item['state'] != 'pending':
                raise Denied('Proposal voting is closed')
            db.execute('INSERT OR IGNORE INTO votes VALUES(?,?,?)', (proposal, ctx.principal, decision))
            votes = {r[0]: r[1] for r in db.execute('SELECT voter,decision FROM votes WHERE proposal=?', (proposal,))}
            status = 'rejected' if 'reject' in votes.values() else ('approved' if all(votes.get(v) == 'approve' for v in voters) else 'pending')
            db.execute('UPDATE proposals SET state=? WHERE id=?', (status, proposal))
            self.c.audit(db, ctx.principal, ctx.task, 'proposal.vote', {'id': proposal, 'version': version, 'decision': decision, 'state': status})
            return status

    def revoke(self, proposal, evidence):
        """Protected administrator entry; never a model tool."""
        if not evidence:
            raise Denied('Revocation evidence required')
        with self.c.lock, self.c.db.transaction() as db:
            row = db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (proposal,)).fetchone()
            if not row:
                raise Denied('Proposal unavailable')
            guard = json.loads(row[0])
            if guard.get('applied_epoch'):
                raise Denied('Already applied policy must be changed through the protected policy entry')
            guard['revoked'] = {'time': utcnow(), 'evidence': evidence}
            db.execute('UPDATE proposal_guards SET body=? WHERE proposal=?', (json.dumps(guard), proposal))
            self.c.audit(db, 'administrator', None, 'proposal.revoked', {'id': proposal, 'evidence': evidence})

    def apply_policy(self, proposal, version, expected_epoch):
        """Protected administrator operation; applies only the exact approved patch."""
        with self.c.lock, self.c.db.transaction() as db:
            state = self.c.policy(db).state
            item = db.execute('SELECT * FROM proposals WHERE id=?', (proposal,)).fetchone()
            prior = db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (proposal,)).fetchone()
            if item and item['version'] == version and prior:
                guard = json.loads(prior[0])
                if guard.get('applied_epoch'):
                    return {'epoch': guard['applied_epoch'], 'duplicate': True, 'current_epoch': state['epoch']}
            guard = self._current(db, item, state, version)
            if state['epoch'] != expected_epoch or item['state'] != 'approved':
                raise Denied('Current policy and approved proposal required')
            voters = json.loads(item['voters'])
            votes = {r[0]: r[1] for r in db.execute('SELECT voter,decision FROM votes WHERE proposal=?', (proposal,))}
            if not all(votes.get(voter) == 'approve' for voter in voters):
                raise Denied('Complete explicit approval required')
            patch = json.loads(item['body']).get('policy_patch')
            if not patch or not set(patch).issubset(POLICY_FIELDS):
                raise Denied('This proposal has no supported policy change')
            if canonical({key: state.get(key) for key in patch}) != canonical(guard['policy_base']):
                raise Denied('Affected policy changed; new proposal required')
            for key, value in patch.items():
                if not isinstance(value, dict if key == 'decision_rules' else list):
                    raise Denied('Invalid policy change shape')
            state.update(patch)
            epoch = self.c._set_policy(db, state, 'proposal:' + proposal + ':' + str(version))
            guard['applied_epoch'] = epoch
            guard['applied_at'] = utcnow()
            db.execute('UPDATE proposal_guards SET body=? WHERE proposal=?', (json.dumps(guard), proposal))
            self.c.audit(db, 'administrator', None, 'proposal.applied', {'id': proposal, 'version': version, 'epoch': epoch})
            return {'epoch': epoch, 'duplicate': False}

    def handoff(self, ctx, assignee, responsible):
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, True)
            for person in (assignee, responsible):
                member = policy.state['members'].get(person, {})
                if not member.get('verified') or not member.get('active'):
                    raise Denied('Handoff person unavailable')
            allowed = policy.state.get('handoff_rules', [])
            if not any(r.get('active') and r.get('approval') and ctx.principal in r['initiators']
                       and assignee in r['assignees'] and responsible == r['responsible'] and ctx.scope in r['scopes'] for r in allowed):
                raise Denied('Handoff authority unavailable')
            old = db.execute('SELECT * FROM handoffs WHERE task=?', (ctx.task,)).fetchone()
            if old:
                return dict(old)  # A new session cannot reset the return counter.
            db.execute('INSERT INTO handoffs VALUES(?,?,?,0,?)', (ctx.task, responsible, assignee, 'pending'))
            return {'task': ctx.task, 'state': 'pending'}

    def return_handoff(self, ctx, task):
        with self.c.lock, self.c.db.transaction() as db:
            self.c._check(db, ctx, True)
            row = db.execute('SELECT * FROM handoffs WHERE task=?', (task,)).fetchone()
            if not row or row['assignee'] != ctx.principal:
                raise Denied('Not the handoff recipient')
            if row['state'] == 'halted':
                return {'state': 'halted', 'notify': row['owner'], 'returns': row['returns']}
            count = row['returns'] + 1
            state = 'halted' if count >= 3 else 'returned'
            db.execute('UPDATE handoffs SET returns=?,state=? WHERE task=?', (count, state, task))
            self.c.audit(db, ctx.principal, task, 'handoff.returned', {'returns': count, 'state': state, 'responsible': row['owner']})
            return {'state': state, 'returns': count, 'notify': row['owner'] if count >= 3 else None}
