"""Pure policy transformation shared by the offline tool and the runtime (落地日志 0108/0120): add a member and a private scope."""
import copy


class Refused(RuntimeError):
    pass


def add_member(state, member, platform, tenant, app, account, evidence, chat, root, personnel_file, union=None, roles=(), team_member=True, extend=False):
    """Return a new policy with `member` and a private scope `<member>-private`.  Raises Refused on any clash."""
    if not (member and account and evidence and chat and tenant and app):
        raise Refused('member, tenant, app, account, chat and evidence are all required')
    state = copy.deepcopy(state)
    scope = member + '-private'
    if scope in state['scopes'] or (member in state['members'] and not extend):
        raise Refused('Member or scope already exists: ' + member)
    if extend and member not in state['members']:
        raise Refused('Unknown member: ' + member)
    if app not in state.get('apps', {}):
        raise Refused('Unknown application: ' + app)
    for other, info in state['members'].items():
        for b in info.get('bindings', []):
            if (b.get('platform'), b.get('tenant'), b.get('app'), b.get('account')) == (platform, tenant, app, account):
                raise Refused('That account is already bound to ' + other)
    for other, info in state['scopes'].items():
        if info.get('transport', {}).get('chat') == chat:
            raise Refused('That conversation already belongs to scope ' + other)
    entry = {'active': True, 'verified': True, 'team_member': team_member, 'roles': list(roles),
             'bindings': [{'platform': platform, 'tenant': tenant, 'app': app, 'account': account, 'evidence': evidence}]}
    if union:
        entry['union_ids'] = [{'platform': platform, 'tenant': tenant, 'id': union, 'evidence': evidence}]
    if extend:      # a member already known by tenant-wide id gains this application's account and a private conversation
        state['members'][member].setdefault('bindings', []).append(entry['bindings'][0])
    else:
        state['members'][member] = entry
    state['scopes'][scope] = {'kind': 'private', 'participants': [member], 'membership_verified': True,
                              'root': str(root), 'personnel_file': str(personnel_file),
                              'transport': {'tenant': tenant, 'apps': [app], 'chat': chat}}
    state['roster_version'] = state.get('roster_version', 0) + 1
    return state
