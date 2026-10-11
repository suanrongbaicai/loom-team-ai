"""Per-person model and reasoning effort, changed from the work panel and effective for the next task (落地日志 0122).

Before, the model lived only in controller.json and a change needed a controller restart.  A row here overrides the configured
value for that person's tasks; no row means "use the configured default".  Only models on the approved list may be set."""
from .policy import Denied, utcnow

DEFAULT_ALLOWED = ('example-model-a', 'example-model-b')
LEVELS = ('low', 'medium', 'high')


def all_rows(db):
    return {r[0]: {'model': r[1], 'reasoning': r[2], 'updated': r[3], 'updated_by': r[4]}
            for r in db.execute('SELECT member,model,reasoning,updated,updated_by FROM member_settings')}


def of(db, member):
    row = db.execute('SELECT model,reasoning FROM member_settings WHERE member=?', (member,)).fetchone()
    return {'model': row[0], 'reasoning': row[1]} if row else {}


def set_model(c, member, model=None, reasoning=None, actor='panel', allowed=DEFAULT_ALLOWED):
    if model is not None and model not in allowed:
        raise ValueError('模型不在已批准的清单里：' + ', '.join(allowed))
    if reasoning is not None and reasoning not in LEVELS:
        raise ValueError('思考强度只能是：' + ', '.join(LEVELS))
    if model is None and reasoning is None:
        raise ValueError('需要模型或思考强度至少一项')
    with c.lock, c.db.transaction() as db:
        if member not in c.db.get_policy(db)['members']:
            raise Denied('没有这个成员')
        db.execute('INSERT INTO member_settings(member,model,reasoning,updated,updated_by) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(member) DO UPDATE SET model=COALESCE(excluded.model,model),'
                   'reasoning=COALESCE(excluded.reasoning,reasoning),updated=excluded.updated,updated_by=excluded.updated_by',
                   (member, model, reasoning, utcnow(), actor))
        c.audit(db, actor, None, 'member.model.set', {'member': member, 'model': model, 'reasoning': reasoning})
        return of(db, member)


def reset(c, member, actor='panel'):
    with c.lock, c.db.transaction() as db:
        db.execute('DELETE FROM member_settings WHERE member=?', (member,))
        c.audit(db, actor, None, 'member.model.reset', {'member': member})
