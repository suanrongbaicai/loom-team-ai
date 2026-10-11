"""Per-group settings: the name people may call the bot by in that group (落地日志 0112)."""
import json

from .policy import Denied, utcnow

MIN, MAX = 2, 12


def alias_of(db, scope):
    row = db.execute('SELECT alias FROM group_settings WHERE scope=?', (scope,)).fetchone()
    return row[0] if row and row[0] else None


def text_of(message):
    if message.get('message_type') != 'text':
        return ''
    try:
        return str(json.loads(message.get('content') or '{}').get('text', ''))
    except (ValueError, AttributeError):
        return ''


def addressed(alias, message):
    """True when a human wrote the group's name for the bot in a text message (plain string match, no model)."""
    return bool(alias) and alias.casefold() in text_of(message).casefold()


def set_alias(c, ctx, alias):
    alias = (alias or '').strip()
    if alias and not (MIN <= len(alias) <= MAX) or any(ch.isspace() or ch in '@＠' for ch in alias):
        raise ValueError('名字需要 %d–%d 个字，不含空格和 @。' % (MIN, MAX))
    with c.lock, c.db.transaction() as db:
        policy = c._check(db, ctx)
        scope = policy.state['scopes'].get(ctx.scope, {})
        if scope.get('kind') != 'group':
            raise Denied('只有群聊可以设置唤起名字；私聊本来就不需要。')
        db.execute('INSERT INTO group_settings(scope,alias,intro_done,set_by,set_at) VALUES(?,?,1,?,?) '
                   'ON CONFLICT(scope) DO UPDATE SET alias=excluded.alias,set_by=excluded.set_by,set_at=excluded.set_at',
                   (ctx.scope, alias or None, ctx.principal, utcnow()))
        c.audit(db, ctx.principal, ctx.task, 'group.alias.set', {'scope': ctx.scope, 'cleared': not alias})
    return {'alias': alias or None, 'meaning': '已保存。之后本群消息里出现这个名字，我会被唤起，不需要 @。' if alias else '已清除。'}


INTRO_BASE = ('这个群刚把你接入，你是第一次在这里出现。先确认现在发言的人是谁（见可信任务信息里的 speaker 和 group_members），'
              '再用 2–4 段短消息（前面的用 finish=false，最后一段 finish=true）做自我介绍，不要一大篇：'
              '①我是谁、能帮大家什么（一两句，结合群里这些人和他们的工作）；'
              '②我看到群里有谁、对每个人了解到什么程度（已认识的说称呼和做什么；还不了解的就如实说不了解）；'
              '③向还不了解的人逐个问“你是谁、做什么、希望我怎么称呼你”，一次问一两位，不搞问卷；他们回答后用 set_my_profile 记下；')
# Only promise what is true today: reading every message needs a platform permission that may not be on yet.
INTRO_MENTION_ONLY = ('④如实说明：目前我只在被 @ 时才能看到这个群里的消息，没被 @ 的话我看不到，也不会保存；不要承诺以后能读到所有发言，也不要问大家想叫我什么。'
                      '最后给一个马上能开始的小事。群里有外部企业的人时不使用团队资料，说明原因。')
INTRO_ALL_MESSAGES = ('④说明两件事：我能读到本群发言并按这个群成员共同可见的范围保存记录；大家可以给我起个名字，之后叫这个名字我就回应，不用 @'
                      '（有人说了名字就用 set_group_alias 保存）；最后给一个马上能开始的小事。群里有外部企业的人时不使用团队资料，说明原因。')


def intro_text(all_messages):
    return INTRO_BASE + (INTRO_ALL_MESSAGES if all_messages else INTRO_MENTION_ONLY)


def intro_once(db, scope, task, all_messages=False):
    """The first-contact note, for the first task that sees this group and for every rules load inside that same task."""
    row = db.execute('SELECT intro_done,intro_task FROM group_settings WHERE scope=?', (scope,)).fetchone()
    if row and row[0] and row[1] != task:
        return None
    db.execute('INSERT INTO group_settings(scope,intro_done,intro_task) VALUES(?,1,?) '
               'ON CONFLICT(scope) DO UPDATE SET intro_done=1,intro_task=COALESCE(intro_task,excluded.intro_task)', (scope, task))
    return intro_text(all_messages)
