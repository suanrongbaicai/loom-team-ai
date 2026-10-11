"""Anyone in our own Feishu enterprise can be recognised from the platform-verified tenant when this ingress is enabled.

The platform tells us which enterprise an account belongs to (tenant_key).  A person from a tenant we already serve is an
internal colleague; that is a platform fact, not a name or a claim.  Such a person is registered automatically as a *basic
member*: verified by that fact and marked as a team member, with no roles or extra grants.  What a basic member may read is still decided by
the ordinary rules (a group's audience is the intersection of everyone's rights), so disclosure still depends on the configured policy, including explicit team_open if enabled.
Upgrading rights is a separate, approved step.  A person from another enterprise is never registered here.

Registration changes the policy (epoch +1), which would cut off tasks that are running.  So it waits until the system is
idle (the event is retried by the gateway, never lost)."""
import hashlib

from .member_labels import set_platform_name
from .policy import Denied, Policy
from .identity_learning import sender_ids
from .onboard_member_core import add_member

PRIVATE_PERSONNEL = """本会话是一位已被飞书确认属于本企业的同事与助手的私聊；按用户的决定，飞书企业内的成员就是团队成员。
可以了解对方做什么、负责什么、希望怎么称呼，并记住。团队资料按现有共享规则使用，不替任何人批准授权，
也不把某个人的私人记录说给别人。需要更多权限时，说明具体缺什么，由负责人批准。大白话交流，先说结论。
"""


class Deferred(RuntimeError):
    """The system is busy; try this message again shortly."""


def member_id(tenant, key):
    return 'u' + hashlib.sha256(('%s|%s' % (tenant, key)).encode()).hexdigest()[:12]


class BasicMembers:
    def __init__(self, control, directory=None):
        self.c = control
        self.directory = directory      # optional: callable(app, open_id) -> display name, best effort

    def served_tenants(self, state):
        return {b.get('tenant') for m in state['members'].values() for b in m.get('bindings', []) if b.get('tenant')}

    def _busy(self, db):
        return bool(db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','preparing','running','ingesting') LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM outbox WHERE state IN ('queued','sending') LIMIT 1").fetchone())

    def _template(self, state, app):
        """A private scope of an existing member that already has approved methods deployed; they are copied to new scopes."""
        with self.c.db.transaction() as db:
            for sid, s in sorted(state['scopes'].items()):
                if s.get('kind') == 'private' and not s.get('auto_basic') \
                        and db.execute('SELECT 1 FROM skill_deployments WHERE scope=? LIMIT 1', (sid,)).fetchone():
                    return sid
        return None

    def from_private_message(self, envelope):
        """First private message from a same-enterprise person who is not on the roster: register and give a private scope."""
        message = envelope['source']['event']['message']
        if message.get('chat_type') != 'p2p':
            return None
        tenant, account, union = sender_ids(envelope)
        app, chat = envelope['bot_id'], message.get('chat_id')
        if not (tenant and account and chat):
            return None
        with self.c.lock:
            with self.c.db.transaction() as db:
                state = self.c.db.get_policy(db)
            if tenant not in self.served_tenants(state):
                return None                                           # another enterprise: the existing unknown-contact path
            existing = None
            try:
                existing = Policy(state).bind_identity('feishu', tenant, app, account, union=union)
            except Denied:
                pass
            if existing and any(s.get('transport', {}).get('chat') == chat for s in state['scopes'].values()):
                return None                                           # known and already has this conversation
            if existing and any(b.get('app') == app and b.get('account') == account
                                for b in state['members'][existing].get('bindings', [])):
                return None
            if existing and not state['members'][existing].get('auto_basic'):
                return None                                           # a real member known by union only: administrators decide
            if any(s.get('transport', {}).get('chat') == chat for s in state['scopes'].values()):
                return None
            template = self._template(state, app)
            if template is None:
                return None
            with self.c.db.transaction() as db:
                if self._busy(db):
                    raise Deferred('Registration waits until running work finishes')
            mid = existing or member_id(tenant, union or account)
            root = self.c.root / 'MemberRecords' / mid
            personnel = self.c.root / ('member-%s-personnel.md' % mid)
            try:
                new = add_member(state, mid, 'feishu', tenant, app, account, 'same Feishu enterprise (platform-verified tenant)',
                                 chat, root, personnel, union=union, team_member=True, extend=bool(existing))
            except RuntimeError:
                return None
            new['scopes'][mid + '-private']['auto_basic'] = True
            root.mkdir(parents=True, exist_ok=True)
            if not personnel.exists():
                personnel.write_text(PRIVATE_PERSONNEL, encoding='utf-8')
            scope = mid + '-private'
            with self.c.db.transaction() as db:
                db.execute('INSERT INTO skill_deployments SELECT ?,skill,version,digest,? FROM skill_deployments WHERE scope=?',
                           (scope, 'deployed', template))
            try:
                self.c.set_policy(new, 'basic member registered: same Feishu enterprise, verified by the platform tenant id')
            except BaseException:
                with self.c.db.transaction() as db:
                    db.execute('DELETE FROM skill_deployments WHERE scope=?', (scope,))
                raise
            if self.directory:
                try:
                    name = self.directory(app, account)
                    with self.c.db.transaction() as db:
                        set_platform_name(db, mid, name)
                except Exception:           # a name is optional
                    pass
            with self.c.db.transaction() as db:
                self.c.audit(db, 'platform-verifier', None, 'member.basic_registered', {'via': 'private message'})
            return mid

    def from_group_listing(self, state, tenant, union, speaker_tenant):
        """Group member list entry: same-enterprise people are registered by their tenant-wide id (no private scope yet)."""
        if tenant != speaker_tenant or tenant not in self.served_tenants(state):
            return None
        for member, m in state['members'].items():
            if any(u.get('tenant') == tenant and u.get('id') == union for u in m.get('union_ids', [])):
                return member
        mid = member_id(tenant, union)
        state['members'][mid] = {'active': True, 'verified': True, 'team_member': True, 'roles': [], 'bindings': [],
                                 'union_ids': [{'platform': 'feishu', 'tenant': tenant, 'id': union,
                                                'evidence': 'same Feishu enterprise (platform-verified tenant) and listed as a group member'}],
                                 'auto_basic': True}
        return mid
