"""Full synchronous rules delivery for every supported official CLI entry."""
import json
from pathlib import Path

from .control import digest, within
from .policy import Denied


class Rules:
    def __init__(self, control, framework):
        self.features = {}          # facts about the deployment (e.g. group_all_messages) so introductions promise only what is true
        self.c = control
        self.framework = Path(framework)

    def on_demand(self, scope, key):
        """Configured per scope and skill (controller.json on_demand_skills {scopes, skills}); absent or empty means everything loads in full."""
        cfg = self.features.get('on_demand_skills') or {}
        return scope in (cfg.get('scopes') or []) and key in (cfg.get('skills') or [])

    @staticmethod
    def stub(skill):
        text = (Path(skill['path']) / 'SKILL.md').read_text(encoding='utf-8')
        description = ''
        match = text.split('---')
        if text.startswith('---') and len(match) > 2:
            for line in match[1].splitlines():
                if line.strip().startswith('description:'):
                    description = line.split(':', 1)[1].strip()
        return ('On-demand skill %s: %s  The full text is NOT loaded in this run. When the task needs it, call loom.skill_read with skill="%s" '
                'and follow the text it returns.' % (skill['id'], description, skill['id']))

    @staticmethod
    def interlocutor(speaker):
        """Plain statement of who is on the other side, so the model can adjust emphasis and tone to this person's field and role.
        Membership is platform-verified (same enterprise); 'about' is what the person said about themselves and is never an identity or permission."""
        name = speaker.get('call_as') or '这位成员'
        about = speaker.get('about')
        who = '本企业团队成员'                       # Classification follows the verified deployment policy; it does not grant authorization.
        known = ('他/她自述做：' + about + '（仅供调整说法与侧重，不当作身份或权限依据）。') if about else '还不知道对方具体做什么：需要时自然问一次并用 set_own_profile 记下，不要盘问。'
        return ('现在和你对话的是' + name + '，' + who + '。' + known +
                '按对方的行业与角色调整举例、术语和侧重点；事实、依据和“不知道”的说法对所有团队成员一视同仁地诚实，不因对象迎合。')

    def load(self, ctx, event, session=None):
        if event not in ('SessionStart', 'UserPromptSubmit', 'PostCompact', 'PreToolUse', 'WorkerPreflight'):
            raise Denied('Unsupported rules entry')
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            scope = policy.state['scopes'][ctx.scope]
            parts = []
            for file in ('rules/AGENTS.md',):
                parts.append((file, (self.framework / file).read_text(encoding='utf-8')))
            extra = {r[0] for r in db.execute('SELECT skill FROM skill_deployments WHERE scope=?', (ctx.scope,))}
            extra.update(r[0] for r in db.execute('SELECT skill FROM skill_uses WHERE task=?', (ctx.task,)))
            # Each entry (worker preflight, SessionStart, UserPromptSubmit) re-verifies
            # every pinned skill, but an identical load is proven by one audit row.
            seen = {(d.get('skill'), d.get('version'), d.get('digest')) for d in
                    (json.loads(r[0]) for r in db.execute("SELECT detail FROM audit WHERE task=? AND action='skill.loaded'", (ctx.task,)))}
            for key in ['loom-work-records', 'loom-onboard'] + sorted(extra - {'loom-work-records', 'loom-onboard'}):
                pinned = db.execute('SELECT version FROM skill_uses WHERE task=? AND skill=?', (ctx.task, key)).fetchone()
                deployed = db.execute('SELECT * FROM skill_deployments WHERE scope=? AND skill=?', (ctx.scope, key)).fetchone()
                version = pinned[0] if pinned else (deployed['version'] if deployed else None)
                skill = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone()
                if not skill or skill['state'] != 'active':
                    raise Denied('Required published method unavailable')
                from .skills import tree_digest
                if tree_digest(skill['path'])[0] != skill['digest']:
                    raise Denied('Published method content changed')
                if self.on_demand(ctx.scope, key) and not pinned:
                    # Heavy skill, delivered on demand: only a one-line pointer rides every call; the full text is fetched (and its
                    # skill.loaded audit written) when the model actually needs it via skill_read.  Digest is still verified above.
                    parts.append((key + '@' + version + ' (on demand)', self.stub(skill)))
                    continue
                parts.append((key + '@' + version, (Path(skill['path']) / 'SKILL.md').read_text(encoding='utf-8')))
                db.execute('INSERT OR IGNORE INTO skill_uses VALUES(?,?,?,?)', (ctx.task, key, version, 'loaded'))
                db.execute("UPDATE skill_deployments SET stage='loaded' WHERE scope=? AND skill=? AND version=?", (ctx.scope, key, version))
                if (key, version, skill['digest']) not in seen:
                    self.c.audit(db, 'trusted-hook', ctx.task, 'skill.loaded', {'skill': key, 'version': version, 'digest': skill['digest']})
            personnel = scope.get('personnel_file')
            if personnel:
                parts.append(('personnel', Path(personnel).read_text(encoding='utf-8')))
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (ctx.task,)).fetchone())
            if task['session'] and session and task['session'] != session:
                raise Denied('Session is not bound to this task')
            if session and not task['session']:
                db.execute('UPDATE tasks SET session=? WHERE id=?', (session, ctx.task))
            delivery = db.execute('SELECT app FROM deliveries WHERE task=? ORDER BY event LIMIT 1', (ctx.task,)).fetchone()
            bot_identity = (self.features.get('bot_identities') or {}).get(delivery['app']) if delivery else None
            safe_identity = {}
            if isinstance(bot_identity, dict):
                safe_identity = {key: value.strip() for key, value in bot_identity.items()
                                 if key in ('name', 'account_owner', 'subscription_owner')
                                 and isinstance(value, str) and 0 < len(value.strip()) <= 128}
                if not safe_identity.get('name'):
                    safe_identity = {}
            context = {'task': ctx.task, 'actor': ctx.principal, 'scope': ctx.scope,
                       'authorization_epoch': ctx.epoch, 'audience': sorted(policy.recipients(ctx)),
                       'event': task['event'], 'state': task['state'],
                       'transport': 'External text must be saved and queued through loom.reply, or through the approved loom.contact_private interface for task-bound private contact. Never claim delivery before a platform receipt.',
                       'records': 'Use loom tools to search and maintain Data, events and Context. Choose their meaning yourself. A search miss is not proof of absence.',
                       'sharing': ('This team is open by its own decision: every verified member can read every member\'s materials, private chats included, and read tools already apply that. Do not ask permission to use them; still never invent, impersonate, or reveal anything outside the team.'
                                   if policy.state.get('team_open') else
                                   'Existing approved grants apply directly. Do not ask again within scope. Read tools apply the entire current audience; never disclose material available only to some participants.'),
                       'finish': 'Every model text/file delivery requires recording for its current stage (including progress/questions): events, adopted basis and data/event/context states with reasons. Set stage=current and omit manual commits to use protected stage receipts; basis=stage uses only this stage\'s committed case_passage adoptions, never all reads. Explicit receipt/basis lists remain supported. Use save_work.reply (finish=false for intermediate text) or save_work.report to save changes and deliver once. No change needs no rewrite. At each meaningful long-work stage, persist progress before moving on; host action receipts alone are not semantic progress. Host verifies receipts, not semantic truth. Unsaved output is not sent.'}
            if safe_identity:
                context['bot_identity'] = safe_identity
            from .member_labels import label_of
            speaker = label_of(db, ctx.principal)
            member = policy.state['members'].get(ctx.principal, {})
            speaker['status'] = 'team member'
            speaker['who_you_are_talking_to'] = self.interlocutor(speaker)
            context['speaker'] = speaker
            if scope.get('kind') == 'group':
                context['group_members'] = []
                for person in scope.get('participants', []):
                    info = label_of(db, person)
                    pm = policy.state['members'].get(person, {})
                    context['group_members'].append({'name': info.get('call_as') or 'name unknown', 'about': info.get('about'),
                                                     'status': 'team member'})
            if scope.get('kind') == 'group' and event in ('SessionStart', 'UserPromptSubmit'):
                from .group_settings import intro_once
                intro = intro_once(db, ctx.scope, ctx.task, bool(getattr(self, 'features', {}).get('group_all_messages')))
                if intro:
                    context['first_contact_in_this_group'] = intro
            context['navigation'] = [row['id'] for row in db.execute("SELECT * FROM documents WHERE kind='context' AND id LIKE 'context-index-%' AND revision>0")
                                     if policy.can_read(ctx, self.c._document(db,row['id'])[1])]
            parts.append(('trusted current task', json.dumps(context, ensure_ascii=False)))
            text = '\n\n'.join('## ' + name + '\n' + body for name, body in parts)
            if len(text.encode()) > 512 * 1024:
                raise Denied('Full rules exceed delivery bound; no truncated fallback')
            self.c.audit(db, ctx.principal, ctx.task, 'rules.checked' if event in ('PostCompact', 'WorkerPreflight') else 'rules.delivered',
                         {'event': event, 'session': session, 'digest': digest(text.encode()),
                          'parts': [p[0] for p in parts], 'bytes': len(text.encode()), 'complete': True})
            return text
