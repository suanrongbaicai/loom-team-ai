"""Worker settings (Windows account, home, jobs, model) for the scope a task runs in.

Configured scopes use their own entry.  A group admitted at runtime has no configured worker, so the task runs with the
worker of the member who is speaking (their own account, their own model login and quota), choosing, among that member's
private scopes, the one that uses the same platform application as the group."""

class ScopeMap(dict):
    """Configured scope -> worker settings, plus the shared worker: the settings of one configured scope (its OS account and
    home) with optional overrides, e.g. the team's default model, so a shared worker need not run on that scope's model."""
    shared = None
    overrides = None

    def __init__(self, items=(), shared=None, overrides=None):
        super().__init__(items)
        self.shared = shared
        self.overrides = dict(overrides or {})

    def shared_settings(self):
        return dict(self[self.shared], **self.overrides)


def resolve(scopes, control, ctx):
    """Worker settings for the task, with the person's own model/reasoning (work panel) laid over the configured ones."""
    settings = _resolve(scopes, control, ctx)
    from .member_settings import of
    with control.db.transaction() as db:
        mine = of(db, ctx.principal)
    return dict(settings, **{k: v for k, v in mine.items() if v}) if mine else settings


def _resolve(scopes, control, ctx):
    shared = getattr(scopes, 'shared', None)        # the team's shared worker, for people who have no worker of their own
    if ctx.scope in scopes:
        return scopes[ctx.scope]
    with control.db.transaction() as db:
        state = control.db.get_policy(db)
    group = state['scopes'].get(ctx.scope)
    if group and group.get('kind') == 'private' and group.get('auto_basic') and shared in scopes:
        return scopes.shared_settings()
    if not group or group.get('kind') != 'group' or not group.get('auto_admitted'):
        raise KeyError(ctx.scope)
    apps = set(group.get('transport', {}).get('apps', []))
    mine = [sid for sid, s in sorted(state['scopes'].items())
            if s.get('kind') == 'private' and s.get('participants') == [ctx.principal] and sid in scopes]
    if not mine:
        if shared in scopes:
            return scopes.shared_settings()
        raise KeyError('no worker is configured for ' + ctx.principal)
    same_app = [sid for sid in mine if apps & set(state['scopes'][sid].get('transport', {}).get('apps', []))]
    return scopes[(same_app or mine)[0]]
