"""Verify model-chosen recording decisions against actual commit receipts."""
import json
import os
from urllib.parse import quote

from .control import digest, identifier
from .policy import Denied


def review(control, ctx, declaration, reply):
    if not isinstance(declaration, dict):
        raise Denied('每次交付需 recording：events、basis，以及本阶段 data/event/context 的实际处理与依据。')
    from .stages import normalize
    declaration = normalize(declaration)
    submitted = declaration
    with control.lock:
        with control.db.transaction() as db:
            policy = control._check(db, ctx)
            key = 'recording-' + digest((ctx.task+':'+reply['id']).encode())
            previous = db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone()
            prior_body = json.loads(control._read(db, previous)) if previous else None
            if prior_body and (prior_body.get('declaration') != declaration or prior_body['reply'] != {k:reply[k] for k in ('document','revision','digest')}):
                raise Denied('同一回复的记录声明发生变化，请核对原提交。')
            queued = db.execute('SELECT id FROM outbox WHERE payload_document=?', (reply['document'],)).fetchone()
            if prior_body and queued:
                return key
            from .stages import expand, observe
            declaration, snapshot = expand(control, ctx, db, submitted, prior_body)
            events, basis = declaration.get('events'), declaration.get('basis')
            if (not isinstance(events, list) or len(events) > 8 or
                    not isinstance(basis, list) or (not snapshot and len(basis) > 40)):
                raise ValueError('Recording events and adopted basis must be bounded lists')
            for event in events:
                identifier(event)
            for item in basis:
                identifier(item['id'] if isinstance(item,dict) else item)
            last = db.execute("SELECT id FROM audit WHERE task=? AND action='records.reviewed' ORDER BY id DESC LIMIT 1", (ctx.task,)).fetchone()
            after = prior_body.get('after_audit', 0) if prior_body else (last[0] if last else 0)
            stage_commits = {json.loads(r[0])['commit'] for r in db.execute(
                "SELECT detail FROM audit WHERE task=? AND id>? AND id<=? AND action='document.committed'",
                (ctx.task, after, snapshot['through_audit'] if snapshot else 9223372036854775807))}
            for key in events:
                try:
                    row, resource = control._document(db, key)
                    policy.require_write(ctx, resource)
                except Denied:
                    raise Denied('recording.events 里的 '+key+' 不是你可写的正式事件记录（可能尚未创建，或不在当前接收范围）。新事项先在 save_work.updates 用 case_passage（event=你自选的新编号，base=0）建立，再把同一编号写入 events；已有事项用 search 得到的准确编号。不要填原消息 message-… 编号。')
                if row['kind'] != 'event' or row['revision'] < 1:
                    raise Denied('events 必须是已保存的正式事件编号，不是 Data 编号或本轮消息编号。')
            resolved = {}
            for layer in ('data', 'event', 'context'):
                value = declaration.get(layer)
                if (not isinstance(value, dict) or value.get('state') not in ('updated', 'no_change', 'pending')
                        or not isinstance(value.get('reason'), str) or not value['reason'].strip()):
                    raise Denied('每层需实际状态与简短理由；无变化无需重写，有缺漏明确 pending。')
                commits = value.get('commits', [])
                if not isinstance(commits, list) or (not snapshot and len(commits) > 20):
                    raise ValueError('Invalid recording receipt list')
                if (value['state'] == 'updated' and not commits) or (value['state'] == 'no_change' and commits):
                    raise Denied('updated 必须列本轮成功提交编号；未改动不能冒称已保存。')
                if layer in ('event','context'):
                    actual = {r[0] for r in db.execute("SELECT c.id FROM commits c JOIN documents d ON d.id=c.document WHERE c.task=? AND c.state='committed' AND d.kind=?", (ctx.task,layer))}
                    if (actual & stage_commits) - set(commits):
                        raise Denied('本阶段已有该层提交未列入声明；请按实际回执填写。')
                resolved[layer] = dict(value, receipts=[])
                for key in commits:
                    c = db.execute('SELECT * FROM commits WHERE id=?', (key,)).fetchone()
                    if not c or c['task'] != ctx.task or c['state'] != 'committed':
                        raise Denied('记录提交不存在、未成功或不属于本轮；先核对实际回执。')
                    row, resource = control._document(db, c['document'])
                    policy.require_read(ctx, resource)
                    if row['kind'] != layer:
                        raise Denied('提交'+str(key)+'属于'+str(row['kind'])+'层，不能列在'+layer+'层（或该文档版本已变）。按层分别列，或用 stage="current" 让程序展开。')
                    content = control._read(db, row) if row['revision'] == c['revision'] else control._history(control._path(db, row), c['revision']).read_bytes()
                    if digest(content) != c['digest']:
                        raise Denied('提交版本原文与凭据不符。')
                    if layer == 'event' and row['id'] not in events:
                        raise Denied('已更新事件未列入正式事件入口。')
                    if layer == 'data' and row['path'].startswith('Data/events/'):
                        event = row['path'][len('Data/events/'):-3]
                        if event not in events:
                            raise Denied('Data 与事件编号不一致；沿原事件追加引用，保留已写历史。')
                    resolved[layer]['receipts'].append({k:c[k] for k in ('id','document','revision','digest')})
            adopted = []
            from .references import read_versions
            stage_uses=[]
            for batch in db.execute("SELECT b.body,b.id FROM citation_batches b JOIN commits c ON c.id=b.id AND c.digest=b.digest AND c.task=b.task AND c.document=b.event WHERE b.task=? AND c.state='committed'",(ctx.task,)):
                item=json.loads(batch['body'])
                if batch['id'] in stage_commits and item['event'] in events:
                    stage_uses.extend(item['adopted'])
            for item in basis:
                key=item['id'] if isinstance(item,dict) else item
                row, resource = control._document(db, key)
                policy.require_read(ctx, resource)
                if not db.execute('SELECT 1 FROM task_reads WHERE task=? AND document=?', (ctx.task, key)).fetchone():
                    raise Denied('采用的依据必须在本轮实际读到，不能只列名称。')
                control._read(db, row)
                revisions = read_versions(db,ctx.task,key)
                if not revisions:
                    raise Denied('缺少本轮读取的具体版本，先用 read 核对依据。')
                revision=item.get('revision',revisions[-1]) if isinstance(item,dict) else revisions[-1]
                if revision not in revisions:
                    raise Denied('该采用版本未在本轮读到。')
                control.version_bytes(db,row,revision)
                uses=[u['occurrence'] for u in stage_uses if (u['id'],u['revision'])==(key,revision)]
                if not uses:
                    raise Denied('缺少已采用依据的CL引用：'+key+' v'+str(revision)+'。用 case_passage 补经过和 citations，再提交本回复；不要删除实际采用的basis来通过检查。只读未采用的资料本就不应列入。')
                adopted.append({'id':key, 'revision':revision,'occurrences':uses})
            task = db.execute('SELECT event FROM tasks WHERE id=?', (ctx.task,)).fetchone()
            original = db.execute('SELECT resource FROM messages WHERE id=?', (task['event'],)).fetchone()
            observe(control, ctx, db, snapshot, adopted, reply['document'])
            body = {'declaration':submitted, 'resolved_declaration':declaration, 'stage_snapshot':snapshot,
                    'task':ctx.task, 'source':original[0] if original else None,
                    'after_audit':after,
                    'events':events, 'layers':resolved, 'adopted':adopted,
                    'reply':{k:reply[k] for k in ('document','revision','digest')},
                    'actions':'Data/actions/' + ctx.task,
                    'delivery':'排队和送达结果由宿主追加动作回执，不能由模型声明成功。'}
            text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
            key = 'recording-' + digest((ctx.task+':'+reply['id']).encode())
            if previous:
                text = control._read(db, previous).decode()
            else:
                control._check(db, ctx, True)
        if not previous:
            control.register(ctx, key, 'data')
            control.commit(ctx, key, 0, text, key, control.lease(ctx, key))
        # Link saved exact response and its layer decisions from the formal
        # business journal. This is a receipt, not a semantic summary.
        for event in events:
            with control.db.transaction() as db:
                held=db.execute('SELECT holder,active FROM leases WHERE document=?',(event,)).fetchone()
                receipt_takeover=bool(held and held['holder']==ctx.task and held['active'])
            # The model has already acquired this CL through the normal lease rules.
            # Its mechanical reply link may follow that acquisition into the paired
            # append-only journal. append_data reads/verifies the old bytes; lease()
            # still rejects an active or unconfirmed writer, even with take_over.
            control.append_data(ctx, event, '\n\n本轮记录与回复凭据：'+key+'；回复原文：'+reply['document']+'。\n',
                                'link-'+digest((key+':'+event).encode()),take_over=receipt_takeover)
            # Small mechanical links below the stage narrative. Semantic prose
            # remains model-written; send receipts arrive in the same action log.
            link_id='delivery-link-'+digest((key+':'+event).encode())
            with control.db.transaction() as db:
                event_row,_=control._document(db,event)
                prior_link=db.execute('SELECT base FROM commits WHERE id=?',(link_id,)).fetchone()
                base=prior_link[0] if prior_link else event_row['revision']
                directory=control._path(db,event_row).parent
                links=[]
                for label,doc in [('回复原文',reply['document']),('记录凭据',key)]:
                    row,_=control._document(db,doc)
                    path=os.path.relpath(control._path(db,row),directory).replace('\\','/')
                    links.append('['+label+'](<'+quote(path,safe='/.-_')+'>)')
                links.append('[动作与送达结果](../Data/actions/'+ctx.task+'/)')
            control.append_event(ctx,event,base,'\n本阶段凭据：'+'；'.join(links)+'。\n',link_id)
        with control.db.transaction() as db:
            control.audit(db, ctx.principal, ctx.task, 'records.reviewed', {'record':key, 'events':events, 'reply':reply['document']})
        return key


def unsent_reports(control, ctx):
    """Reports this task saved whose file reply never reached the outbox. One warning per task, then the model decides.

    Saved -> bound (file_replies) -> queued (outbox row for THIS task) -> sent are different facts. A binding written before
    a failed recording review is not a queued reply. Any outbox row, whatever its later state (sent, unknown, failed,
    cancelled, needs_reauthorization), means the report entered the delivery protocol and is never re-warned or re-sent here."""
    with control.lock, control.db.transaction() as db:
        control._check(db, ctx)                      # current task binding and audience, re-verified under the lock
        saved = [json.loads(r[0])['report'] for r in db.execute("SELECT detail FROM audit WHERE task=? AND action='report.saved'", (ctx.task,))]
        pending = [r for r in dict.fromkeys(saved)
                   if not db.execute('SELECT 1 FROM file_replies f JOIN outbox o ON o.payload_document=f.reply_document '
                                     'WHERE f.report_document=? AND o.task=?', (r, ctx.task)).fetchone()]
        if not pending or db.execute("SELECT 1 FROM audit WHERE task=? AND action='report.unsent_warned'", (ctx.task,)).fetchone():
            return None
        control.audit(db, ctx.principal, ctx.task, 'report.unsent_warned', {'reports': pending})
    return pending


def deliver(control, ctx, text, key, declaration, finish=False, prepare=None):
    """One model answer -> saved exact payload -> reviewed records -> outbox."""
    if prepare is None and isinstance(text, str):
        # FIRST, before any other check or save: a credential is refused (and never echoed) BEFORE the link guard can quote anything from the text;
        # then a mistyped report/copy link is refused.  Nothing is saved or sent on either refusal.
        from .secret_guard import guard as secret_guard
        secret_guard(control, ctx, text)
        from .link_guard import guard as link_guard
        link_guard(control, ctx, text)
    if not isinstance(declaration, dict):
        raise Denied('文字、进度和文件交付均须先提供本阶段 recording，不能只保存答案就发送。')
    if prepare is None and finish:
        pending = unsent_reports(control, ctx)
        if pending:
            raise Denied('本任务已保存但还没有发送的报表：' + '、'.join(pending) + '。文字回复不会带上文件；要交付报表，请用 save_work.report（新的交付 id、report=上面的编号、recording）或 send_report 发送。'
                         '若确实不打算发送，请在回复里如实说明后再回复一次（这个提醒每个任务只出现一次）。')
    from .stages import normalize
    declaration = normalize(declaration)
    with control.lock:
        # Check mechanical stage mistakes before reserving a reply or attachment.
        # The full adoption/version/audience review still runs below.
        prior = None
        if declaration.get('stage'):
            from .stages import expand
            with control.db.transaction() as db:
                old_reply = db.execute('SELECT id FROM commits WHERE document=? AND task=?', (key, ctx.task)).fetchone()
                old_record = (db.execute('SELECT * FROM documents WHERE id=?',
                              ('recording-' + digest((ctx.task+':'+old_reply['id']).encode()),)).fetchone()
                              if old_reply else None)
                prior = json.loads(control._read(db, old_record)) if old_record else None
                expand(control, ctx, db, declaration, prior)
        if prepare is None:
            from .public_web import source_footer
            footer_declaration = declaration
            if declaration.get('stage'):
                from .stages import expand
                with control.db.transaction() as db:
                    old_reply = db.execute('SELECT id FROM commits WHERE document=? AND task=?', (key, ctx.task)).fetchone()
                    old_record = (db.execute('SELECT * FROM documents WHERE id=?',
                                  ('recording-' + digest((ctx.task+':'+old_reply['id']).encode()),)).fetchone()
                                  if old_reply else None)
                    prior = json.loads(control._read(db, old_record)) if old_record else None
                    footer_declaration, _ = expand(control, ctx, db, declaration, prior)
            text=source_footer(control,ctx,text,footer_declaration)
        saved = control.save_reply(ctx, text, key)
        # Existing attachment flows retain their order. Native images explicitly
        # request review-first so a bad declaration never pins image delivery.
        if prepare and not getattr(prepare,'review_first',False):
            prepare(saved)
        review(control, ctx, declaration, saved)
        if prepare and getattr(prepare,'review_first',False):
            prepare(saved)
        control.flush_actions(ctx)
        return {'outbox':control.queue_reply(ctx, saved['id'], finish), 'state':'queued-not-delivered'}
