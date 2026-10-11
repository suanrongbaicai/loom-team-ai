"""Bounded model access to preserved action originals via a reviewed receipt."""
import json
from .control import within
from .policy import Denied


def read(c,ctx,recording,after=0):
    if type(after) is not int or after<0:raise ValueError('Invalid action cursor')
    c.read(ctx,recording)
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx)
        proof=None
        for row in db.execute("SELECT task,detail FROM audit WHERE action='records.reviewed'"):
            if json.loads(row['detail']).get('record')==recording:proof=row;break
        if proof is None:raise Denied('Verified recording receipt required')
        task=db.execute('SELECT scope FROM tasks WHERE id=?',(proof['task'],)).fetchone()
        # The complete action log includes even read-but-not-adopted sources.
        # A grant for just the answer cannot silently grant that wider history.
        for row in db.execute('SELECT document FROM task_reads WHERE task=?',(proof['task'],)):
            policy.require_read(ctx,c._document(db,row['document'])[1])
        rows=db.execute('SELECT * FROM audit WHERE task=? AND id>? ORDER BY id LIMIT 51',
                        (proof['task'],after)).fetchall()
        originals=[]
        for row in rows[:50]:
            path=within(policy.state['scopes'][task['scope']]['root'],f'Data/actions/{proof["task"]}/{row["id"]}.json')
            saved=json.loads(path.read_text(encoding='utf-8'))
            if saved!=dict(row):raise Denied('Saved action original differs')
            originals.append(saved)
        c.audit(db,ctx.principal,ctx.task,'actions.read',{'recording':recording,'after':after,'count':len(originals)})
        return {'actions':originals,'next_after':rows[49]['id'] if len(rows)>50 else None,
                'coverage':'本次任务已保存动作原件；发送结果以reply.sent回执为准。'}
