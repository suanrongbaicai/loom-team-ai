"""Content grants, not directory labels, decide readable and disclosable material.

This module runs only inside the protected controller. Workers cannot supply a
Context or mutate the policy. OS ACL enforcement is an additional deployment gate.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


class Denied(PermissionError):
    pass


class StaleContext(Denied):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed


@dataclass(frozen=True)
class Context:
    principal: str
    scope: str
    epoch: int
    task: str


@dataclass(frozen=True)
class Resource:
    id: str
    owner: str
    scope: str
    kind: str
    created_at: str
    audience: tuple = ()


class Policy:
    def __init__(self, state: Mapping):
        self.state = state
        self.epoch = state['epoch']

    def recipients(self, ctx: Context):
        if ctx.epoch != self.epoch:
            raise StaleContext('Authorization or audience changed; rebuild context')
        member = self.state['members'].get(ctx.principal)
        scope = self.state['scopes'].get(ctx.scope)
        if not member or not member.get('active') or not member.get('verified'):
            raise Denied('Unverified or inactive identity')
        if not scope or ctx.principal not in scope['participants']:
            raise Denied('Scope unavailable')
        audience = scope['participants']
        if not audience or not scope.get('membership_verified'):
            raise Denied('Unverified audience')
        if scope.get('membership_expires_at') and instant(scope['membership_expires_at']) <= datetime.now(timezone.utc):
            raise Denied('Audience verification expired')
        if any(not self.state['members'].get(p, {}).get('active') or not self.state['members'].get(p, {}).get('verified') for p in audience):
            raise Denied('Audience has inactive or unknown members')
        if scope['kind'] == 'private' and audience != [ctx.principal]:
            raise Denied('Private scope must bind one person')
        return frozenset(audience)

    def can_read(self, ctx: Context, resource: Resource):
        audience = self.recipients(ctx)
        source = self.state['scopes'].get(resource.scope)
        if not source:
            return False
        # A native group artifact is readable only in its own verified scope.
        # Personal ownership alone cannot open it into another audience.
        return all((resource.scope == ctx.scope and person in resource.audience)
                   or self._person_allowed(person, ctx.scope, resource) for person in audience)

    def _person_allowed(self, person: str, destination: str, resource: Resource):
        if person == resource.owner and self.state['scopes'][resource.scope]['kind'] == 'private':
            return True
        if self.state.get('team_open'):
            # An administrator explicitly enabled sharing among verified active members, including private records.
            # Only verified active members are ever in an audience (recipients() enforces that), so this never reaches an outsider.
            member = self.state['members'].get(person, {})
            if member.get('active') and member.get('verified'):
                return True
        now = datetime.now(timezone.utc)
        for grant in self.state.get('grants', []):
            if not grant.get('active') or not grant.get('approval'):
                continue
            if instant(grant['starts_at']) > now or (grant.get('expires_at') and instant(grant['expires_at']) <= now):
                continue
            if destination not in grant['destinations'] or resource.owner not in grant['owners']:
                continue
            if resource.kind not in grant['kinds']:
                continue
            if resource.scope not in grant['source_scopes']:
                continue
            if grant['mode'] == 'on_demand' and resource.id not in grant['resources']:
                continue
            if grant['mode'] not in ('on_demand', 'all_work', 'hierarchy'):
                continue
            created = instant(resource.created_at)
            approved = instant(grant['starts_at'])
            if created < approved and not grant.get('include_history'):
                continue
            if created >= approved and not grant.get('include_future'):
                continue
            if person in grant.get('recipients', []):
                return True
            if grant['mode'] == 'hierarchy':
                roles = set(self.state['members'][person].get('roles', []))
                if roles.intersection(grant.get('roles', [])):
                    return True
        return False

    def require_read(self, ctx, resource):
        if not self.can_read(ctx, resource):
            # No restricted filename, owner or existence in the error.
            raise Denied('Resource unavailable in this audience')

    def require_write(self, ctx, resource):
        self.recipients(ctx)
        if ctx.scope != resource.scope:
            raise Denied('Shared read permission does not grant source write access')
        self.require_read(ctx, resource)

    def bind_identity(self, platform, tenant, app, account, union=None):
        """The member bound to this account in this application; otherwise, when the platform supplies a tenant-wide
        person id (Feishu union_id), the one member who has already been shown to own it.  Never guesses."""
        key = (platform, tenant, app, account)
        found = [p for p, m in self.state['members'].items()
                 if account is not None and m.get('active') and m.get('verified')
                 and any(tuple(b.get(k) for k in ('platform', 'tenant', 'app', 'account')) == key
                         and b.get('evidence') for b in m.get('bindings', []))]
        if len(found) == 1:
            return found[0]
        if not found and union:
            found = [p for p, m in self.state['members'].items()
                     if m.get('active') and m.get('verified')
                     and any(u.get('platform', 'feishu') == platform and u.get('tenant') == tenant and u.get('id') == union
                             and u.get('evidence') for u in m.get('union_ids', []))]
            if len(found) == 1:
                return found[0]
        raise Denied('Identity binding unavailable or ambiguous')
