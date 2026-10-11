"""A group the bot has been added to becomes a working scope on its own, when it is safe to.

Safe means: a verified roster member @-mentions or replies to the bot in a group that has no scope yet, and *every* current
member of that group is a verified roster member (recognised by tenant-wide id, so it does not matter which application each
person uses).  The scope's audience is exactly those members, so replies and reads are limited to what all of them may see.
If anyone in the group is not on the roster the group is not admitted, nothing is disclosed, and the attempt is audited.
Nothing here is chosen by the model; the scope, its records folder and personnel file are created by trusted code."""
import hashlib
from pathlib import Path
import time

from .policy import Denied, Policy
from .identity_learning import sender_ids

GROUP_PERSONNEL = """本会话是一个团队群，成员均为已核验的团队成员，群里的回答和读取以全体现任成员都有权看的内容为限。
群成员变化时，受众自动跟着群走；一旦群里出现未登记的人，在他离开或登记之前，不使用团队资料回答。
群里的发言人用自己的账号和额度办事。资料默认按现有共享规则使用，不能替人批准授权。
大白话交流，只追问影响下一步的缺口，非必要不重复。发出的文字以实际送达回执为准。
"""


def group_scope_id(tenant, chat):
    return 'group-' + hashlib.sha256(('%s|%s' % (tenant, chat)).encode()).hexdigest()[:12]


class GroupAdmission:
    def __init__(self, control, membership, basic=None):
        self.c, self.membership = control, membership
        self.basic = basic          # BasicMembers: same-enterprise people listed in the group are registered, others refuse
        self._followed = {}         # scope -> time of the last roster follow-up (rate limit)

    def _audit(self, action, detail):
        with self.c.db.transaction() as db:
            self.c.audit(db, 'platform-verifier', None, action, detail)

    def relevant(self, envelope):
        """Is this group message for us?  Yes if the group already is a scope (its own messages are kept as context, the
        group's name for the bot may call us), or if the bot is @-mentioned.  Everything else in a group we merely sit in
        is ignored before anything is read, learned, counted or stored."""
        source = envelope['source']
        message = source['event']['message']
        tenant = source['header'].get('tenant_key') or source['event']['sender'].get('tenant_key')
        app, chat = envelope['bot_id'], message.get('chat_id')
        with self.c.db.transaction() as db:
            state = self.c.db.get_policy(db)
        for scope in state['scopes'].values():
            transport = scope.get('transport', {})
            if transport.get('chat') == chat and transport.get('tenant') == tenant and app in transport.get('apps', []):
                return True
        bot = state.get('apps', {}).get(app, {}).get('open_id')
        return bool(bot) and any((m.get('id') or {}).get('open_id') == bot for m in message.get('mentions', []))

    def _skill_source(self, state, speaker, app):
        """The speaker's own private scope (same application first) that already has approved methods deployed."""
        mine = [sid for sid, s in sorted(state['scopes'].items()) if s.get('kind') == 'private' and s.get('participants') == [speaker]]
        mine.sort(key=lambda sid: app not in state['scopes'][sid].get('transport', {}).get('apps', []))
        with self.c.db.transaction() as db:
            for sid in mine + sorted(s for s, v in state['scopes'].items() if v.get('kind') == 'private' and not v.get('auto_basic')):
                if db.execute('SELECT 1 FROM skill_deployments WHERE scope=? LIMIT 1', (sid,)).fetchone():
                    return sid
        return None

    def _ensure_skills(self, state, sid, account, union, app):
        """A group admitted before it inherited methods gets them from the speaker's own scope the next time it is used."""
        with self.c.db.transaction() as db:
            if db.execute('SELECT 1 FROM skill_deployments WHERE scope=? LIMIT 1', (sid,)).fetchone():
                return
        tenant = state['scopes'][sid]['transport']['tenant']
        try:
            speaker = Policy(state).bind_identity('feishu', tenant, app, account, union=union)
        except Denied:
            return
        source = self._skill_source(state, speaker, app)
        if source is None:
            return
        with self.c.db.transaction() as db:
            db.execute('INSERT OR IGNORE INTO skill_deployments SELECT ?,skill,version,digest,? FROM skill_deployments WHERE scope=?',
                       (sid, 'deployed', source))
            self.c.audit(db, 'platform-verifier', None, 'group.methods.inherited', {'scope': sid, 'from': source})

    FOLLOW_SECONDS = 10

    def _follow_roster(self, state, sid, scope, app, tenant):
        """O9 (user decision 2026-10-03: members of our Feishu enterprise are team members).  A group that was already admitted keeps working when
        same-enterprise colleagues are added later: they are registered from the platform's member list (tenant-wide id, as at admission) BEFORE the message is
        taken in, instead of the group going silent until each of them privately messages the bot.  People of another enterprise are never registered (the audience
        check keeps the group silent).  Waits for an idle system like every roster change (the gateway retries the event); rate-limited per group; never raises
        anything but Deferred."""
        if not self.basic:
            return
        now = time.monotonic()
        if now - self._followed.get(sid, -1e9) < self.FOLLOW_SECONDS:
            return
        client = self.membership.clients.get(app)
        chat = scope.get('transport', {}).get('chat')
        if client is None or not chat:
            return
        try:
            listed = client.members(chat, 'union_id')
        except Exception:
            return                                          # the audience check reports the real problem
        fresh = {**state, 'members': {k: dict(v) for k, v in state['members'].items()}}
        added = {}
        for item in listed:
            try:
                Policy(fresh).bind_identity('feishu', item['tenant'], app, None, union=item['account'])
            except Denied:
                if item['tenant'] != tenant:
                    continue
                person = self.basic.from_group_listing(fresh, item['tenant'], item['account'], tenant)
                if person:
                    added[person] = item.get('name')
        self._followed[sid] = now
        if not added:
            return
        with self.c.db.transaction() as db:
            busy = db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','preparing','running','ingesting') LIMIT 1").fetchone()
        if busy:
            from .basic_members import Deferred
            self._followed.pop(sid, None)
            raise Deferred('Registration waits until running work finishes')
        self.c.set_policy(fresh, 'group members registered: same Feishu enterprise (platform-verified tenant id), listed in an admitted group')
        with self.c.db.transaction() as db:
            from .member_labels import set_platform_name
            for person, name in added.items():
                if name:
                    set_platform_name(db, person, name)
            self.c.audit(db, 'platform-verifier', None, 'group.members.registered', {'scope': sid, 'count': len(added)})

    def ensure(self, envelope):
        source = envelope['source']
        message = source['event']['message']
        if message.get('chat_type') != 'group':
            return None
        tenant, account, union = sender_ids(envelope)
        app, chat = envelope['bot_id'], message.get('chat_id')
        if not (tenant and chat):
            return None
        with self.c.lock:
            with self.c.db.transaction() as db:
                state = self.c.db.get_policy(db)
            for sid, scope in state['scopes'].items():
                transport = scope.get('transport', {})
                if transport.get('chat') == chat and transport.get('tenant') == tenant and app in transport.get('apps', []):
                    if scope.get('auto_admitted'):
                        self._ensure_skills(state, sid, account, union, app)
                        self._follow_roster(state, sid, scope, app, tenant)
                    return sid
            policy = Policy(state)
            speaker = None
            try:
                speaker = policy.bind_identity('feishu', tenant, app, account, union=union)
            except Denied:
                if not (self.basic and union and tenant in self.basic.served_tenants(state)):
                    return None                              # not a roster member: the existing unknown-contact path handles it
            client = self.membership.clients.get(app)
            if client is None:
                self._audit('group.admission.unavailable', {'reason': 'no platform client for the application'})
                return None
            try:
                listed = client.members(chat, 'union_id')
            except Exception as error:
                self._audit('group.admission.unavailable', {'reason': type(error).__name__})
                return None
            people, registered, names = set(), False, {}
            fresh = {**state, 'members': {k: dict(v) for k, v in state['members'].items()}}
            for item in listed:
                try:
                    person = Policy(fresh).bind_identity('feishu', item['tenant'], app, None, union=item['account'])
                except Denied:
                    person = self.basic.from_group_listing(fresh, item['tenant'], item['account'], tenant) if self.basic else None
                    if person is None:
                        self._audit('group.admission.refused', {'reason': 'a member is not on the roster and not from this enterprise',
                                                                'members': len(listed)})
                        return None
                    registered = True
                people.add(person)
                names[person] = item.get('name')
            if registered:
                with self.c.db.transaction() as db:
                    busy = db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','preparing','running','ingesting') LIMIT 1").fetchone()
                if busy:
                    from .basic_members import Deferred
                    raise Deferred('Registration waits until running work finishes')
                state = fresh
            if speaker is None:      # a same-enterprise colleague registered just now by the group listing
                speaker = next((p for p in people if any(u.get('tenant') == tenant and u.get('id') == union
                                                         for u in state['members'][p].get('union_ids', []))), None)
            if speaker not in people:
                self._audit('group.admission.refused', {'reason': 'speaker is not in the listed members'})
                return None
            sid = group_scope_id(tenant, chat)
            root = Path(self.c.root) / 'GroupRecords' / sid
            personnel = Path(self.c.root) / ('group-%s-personnel.md' % sid)
            root.mkdir(parents=True, exist_ok=True)
            if not personnel.exists():
                personnel.write_text(GROUP_PERSONNEL, encoding='utf-8')
            source = self._skill_source(state, speaker, app)
            if source is None:
                self._audit('group.admission.unavailable', {'reason': 'speaker has no private scope with deployed methods'})
                return None
            state = {**state, 'scopes': dict(state['scopes'])}
            state['scopes'][sid] = {'kind': 'group', 'participants': sorted(people), 'membership_verified': False,
                                    'root': str(root), 'personnel_file': str(personnel), 'auto_admitted': True,
                                    'transport': {'tenant': tenant, 'apps': [app], 'chat': chat}}
            with self.c.db.transaction() as db:      # the group works with the same approved methods the speaker's own scope has
                db.execute('DELETE FROM skill_deployments WHERE scope=?', (sid,))
                db.execute('INSERT INTO skill_deployments SELECT ?,skill,version,digest,? FROM skill_deployments WHERE scope=?',
                           (sid, 'deployed', source))
            try:
                self.c.set_policy(state, 'group admitted automatically: the bot was added, every current member is a verified roster member')
            except BaseException:
                with self.c.db.transaction() as db:
                    db.execute('DELETE FROM skill_deployments WHERE scope=?', (sid,))
                raise
        with self.c.db.transaction() as db:      # the platform's display names, only for addressing people
            for person, name in names.items():
                if name:
                    from .member_labels import set_platform_name
                    set_platform_name(db, person, name)
        self.membership.verify(sid, force=True)
        return sid
