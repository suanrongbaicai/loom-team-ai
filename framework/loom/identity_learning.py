"""Learn a person's tenant-wide platform id from a message the platform has already authenticated.

A member is bound to an account in each application separately, and Feishu gives the same person a different open_id in
every application.  The event itself also carries the person's tenant-wide id (union_id).  When a message arrives from an
account that is already bound to a verified member, that union_id is recorded on the member, with its evidence, so the same
person is recognised in another application (for example in a group the bot was added to).  An id already owned by a
different member is a conflict: nothing is written and the conflict is audited."""
from .policy import Denied, Policy


def sender_ids(envelope):
    source = envelope['source']
    header, sender = source['header'], source['event']['sender']
    ids = sender.get('sender_id') or {}
    tenant = header.get('tenant_key') or sender.get('tenant_key')
    return tenant, ids.get('open_id'), ids.get('union_id')


def learn(control, envelope):
    try:
        tenant, account, union = sender_ids(envelope)
    except (KeyError, TypeError):
        return None
    if not (tenant and account and isinstance(union, str) and union):
        return None
    app = envelope['bot_id']
    with control.lock:
        with control.db.transaction() as db:
            state = control.db.get_policy(db)
        try:
            person = Policy(state).bind_identity('feishu', tenant, app, account)      # exact binding only: this is the proof
        except Denied:
            return None
        mine = state['members'][person].setdefault('union_ids', [])
        if any(u.get('tenant') == tenant and u.get('id') == union for u in mine):
            return None
        for other, member in state['members'].items():
            if other != person and any(u.get('tenant') == tenant and u.get('id') == union for u in member.get('union_ids', [])):
                with control.db.transaction() as db:
                    control.audit(db, 'platform-verifier', None, 'identity.union_conflict', {'member': person})
                return None
        mine.append({'platform': 'feishu', 'tenant': tenant, 'id': union, 'app': app,
                     'evidence': 'tenant-wide id carried by a platform-authenticated message from the bound account'})
        control.set_policy(state, 'identity learning: a bound member\'s tenant-wide id recorded from their own authenticated message')
        return person
