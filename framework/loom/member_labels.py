"""How to address a person (落地日志 0119/0120).  A label is NOT identity: identity comes only from platform account ids and
decides permissions; a label (the platform's display name, or what the person asks to be called, and what they say they do)
is only for speaking to them.  Nothing here grants anything."""
from .policy import Denied, utcnow

MAX_NAME, MAX_ABOUT = 24, 300


def _clean(text, limit):
    text = ' '.join(str(text or '').split())
    return text[:limit] or None


def set_platform_name(db, member, name, source='feishu'):
    """Record the platform's display name (does not overwrite what the person chose to be called)."""
    name = _clean(name, MAX_NAME)
    if not name:
        return
    db.execute('INSERT INTO member_labels(member,display_name,display_source,updated) VALUES(?,?,?,?) '
               'ON CONFLICT(member) DO UPDATE SET display_name=excluded.display_name,display_source=excluded.display_source,updated=excluded.updated',
               (member, name, source, utcnow()))


def set_own_profile(c, ctx, call_name=None, about=None):
    """A person tells the assistant how to call them and what they do.  Only about themselves (the speaker of this task)."""
    call_name, about = _clean(call_name, MAX_NAME), _clean(about, MAX_ABOUT)
    if not (call_name or about):
        raise ValueError('需要称呼或自我介绍至少一项')
    with c.lock, c.db.transaction() as db:
        c._check(db, ctx)
        db.execute('INSERT INTO member_labels(member,call_name,about,updated) VALUES(?,?,?,?) '
                   'ON CONFLICT(member) DO UPDATE SET call_name=COALESCE(excluded.call_name,call_name),'
                   'about=COALESCE(excluded.about,about),updated=excluded.updated',
                   (ctx.principal, call_name, about, utcnow()))
        c.audit(db, ctx.principal, ctx.task, 'member.profile.set', {'call_name': bool(call_name), 'about': bool(about)})
    return {'saved': True, 'meaning': '已记住。以后我会这样称呼你，并记得你做什么。'}


def label_of(db, member):
    row = db.execute('SELECT display_name,call_name,about FROM member_labels WHERE member=?', (member,)).fetchone()
    if not row:
        return {}
    return {'call_as': row[1] or row[0], 'platform_name': row[0], 'about': row[2]}
