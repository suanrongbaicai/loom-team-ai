"""Protected stage receipts and an observational text-source ledger.

Stored in the existing append-only audit. Snapshots never grow after issuance;
they do not grant access, infer adoption, or impose a new delivery gate.
"""
import copy
import json

from .control import digest
from .policy import Denied


def _merge(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def normalize(declaration):
    """basis='stage' without a stage means the current stage (the only reading that makes sense); return a copy."""
    if isinstance(declaration, dict):
        basis = declaration.get('basis')
        if isinstance(basis, list):
            for item in basis:
                if item == 'stage' or isinstance(item, dict) and item.get('id') == 'stage':
                    raise ValueError('recording.basis 的 stage 是保留值：用 basis="stage", stage="current"，不能用 ["stage"] 或 {"id":"stage"}。宿主展开本阶段已采用凭据，不要编文档或commit编号；逐条依据则填真实已读 id/revision。')
                if not isinstance(item, str) and not (isinstance(item, dict) and set(item) == {'id','revision'} and isinstance(item['id'], str) and type(item['revision']) is int and item['revision'] >= 1):
                    raise ValueError('recording.basis 逐条依据须为真实文档 id 或 {id,revision>=1}；本阶段凭据用字符串 "stage"。')
        elif basis != 'stage':
            raise ValueError('recording.basis 须为真实依据列表或字符串 "stage"。')
    if isinstance(declaration, dict) and declaration.get('basis') == 'stage' and not declaration.get('stage'):
        return dict(declaration, stage='current')
    return declaration


def capture(c, ctx, db):
    policy = c._check(db, ctx)
    last = db.execute("SELECT id FROM audit WHERE task=? AND action='records.reviewed' ORDER BY id DESC LIMIT 1",
                      (ctx.task,)).fetchone()
    after = last[0] if last else 0
    rows = db.execute("SELECT id,action,detail FROM audit WHERE task=? AND id>? "
                      "AND action IN ('document.read','document.search','document.committed') ORDER BY id",
                      (ctx.task, after)).fetchall()
    through = rows[-1]['id'] if rows else after
    receipts, sources = [], {}
    for row in rows:
        detail = json.loads(row['detail'])
        if row['action'] == 'document.committed':
            saved = db.execute("SELECT c.*,d.kind,d.path FROM commits c JOIN documents d ON d.id=c.document "
                               "WHERE c.id=? AND c.task=? AND c.state='committed'",
                               (detail['commit'], ctx.task)).fetchone()
            if saved and saved['kind'] in ('data', 'event', 'context'):
                policy.require_read(ctx, c._document(db, saved['document'])[1])
                receipts.append({k: saved[k] for k in ('id','document','kind','revision','digest','path')})
            continue
        if row['action'] == 'document.read':
            items = [{'id': detail['resource'], 'revision': detail['revision'], 'ranges': detail.get('ranges'),
                      'type': 'range' if detail.get('partial') else 'full_text'}]
        else:
            items = [dict(item, type='search_snippet' if detail.get('partial') else 'full_text')
                     for item in detail.get('returned', [])]
        for item in items:
            doc, resource = c._document(db, item['id'])
            policy.require_read(ctx, resource)
            lineage = db.execute('SELECT source,part FROM materials WHERE document=?', (item['id'],)).fetchone()
            # This is attachment provenance, not a declaration of having read
            # the attached bytes or any linked underlying source.
            lineage = dict(lineage) if lineage and policy.can_read(ctx, c._document(db, lineage['source'])[1]) else None
            key = (item['id'], item['revision'], item['type'], json.dumps(lineage, sort_keys=True))
            if key not in sources:
                sources[key] = {'id': item['id'], 'revision': item['revision'], 'display_type': item['type'],
                                'ranges': [], 'material_lineage': lineage, 'legacy_range_unknown': False}
            if item.get('ranges') is None:
                sources[key]['legacy_range_unknown'] = True
            else:
                sources[key]['ranges'].extend(item['ranges'])
    candidates = []
    for value in sources.values():
        value['ranges'] = _merge(value['ranges'])
        value['candidate'] = digest(json.dumps(value, sort_keys=True).encode())
        candidates.append(value)
    body = {'task': ctx.task, 'actor': ctx.principal, 'scope': ctx.scope, 'epoch': ctx.epoch,
            'after_audit': after, 'through_audit': through, 'receipts': receipts, 'candidates': candidates}
    token = 'stage-' + digest(json.dumps(body, sort_keys=True).encode())
    old = db.execute("SELECT 1 FROM audit WHERE task=? AND action='stage.snapshot' "
                     "AND json_extract(detail,'$.token')=?", (ctx.task, token)).fetchone()
    if not old:
        c.audit(db, ctx.principal, ctx.task, 'stage.snapshot', {'token': token, 'body': body})
    return token, body


def load(c, ctx, db, token):
    c._check(db, ctx)
    row = db.execute("SELECT detail FROM audit WHERE task=? AND action='stage.snapshot' "
                     "AND json_extract(detail,'$.token')=? ORDER BY id DESC LIMIT 1", (ctx.task, token)).fetchone()
    if not row and isinstance(token, str) and token.startswith('stage-') and len(token) >= 26:
        # A long token copied incompletely: accept it only if it names exactly one of THIS task's snapshots.
        # Identity, epoch and read checks below still apply; a wrong or mixed-up middle is still refused.
        found = {json.loads(r[0])['token'] for r in db.execute("SELECT detail FROM audit WHERE task=? AND action='stage.snapshot'", (ctx.task,))
                 if json.loads(r[0])['token'].startswith(token)}
        if len(found) == 1:
            return load(c, ctx, db, found.pop())
    if not row:
        raise Denied('阶段凭据不可用（长编号抄写容易漏字或错字）。改用 stage="current" 让程序取当前阶段，或从 stage_receipt 的返回里原样复制。')
    body = json.loads(row['detail'])['body']
    if (body['task'], body['actor'], body['scope'], body['epoch']) != (ctx.task, ctx.principal, ctx.scope, ctx.epoch):
        raise Denied('阶段凭据身份或权限代次已变化。')
    policy = c.policy(db)
    for item in body['receipts']:
        policy.require_read(ctx, c._document(db, item['document'])[1])
    for item in body['candidates']:
        policy.require_read(ctx, c._document(db, item['id'])[1])
    return body


def inspect(c, ctx, token=None, offset=0):
    if type(offset) is not int or offset < 0 or (offset and not token):
        raise ValueError('分页需要原阶段凭据及非负offset。')
    if token == 'current':
        if offset:
            # Pages of one batch must come from a frozen snapshot; 'current' would re-capture and shift the pages.
            raise ValueError('分页需要原阶段凭据（首屏可用current，之后用返回的stage续页）。')
        token = None      # same meaning as recording.stage='current': the current stage
    with c.lock, c.db.transaction() as db:
        token, body = (token, load(c, ctx, db, token)) if token else capture(c, ctx, db)
        size = 40
        candidates = body['candidates'][offset:offset+size]
        return {'stage': token, 'after_audit': body['after_audit'], 'through_audit': body['through_audit'],
                'commits_by_layer': {kind: sum(r['kind']==kind for r in body['receipts']) for kind in ('data','event','context')},
                'candidates': candidates, 'candidate_count': len(body['candidates']),
                'next_offset': offset+size if offset+size < len(body['candidates']) else None,
                'mode': 'shadow', 'coverage': '仅本阶段document.read/search实际返回的文字范围；附件描述不等于文件正文已读。未覆盖图像理解、间接来源及会话直接输入，不代表全部采用候选。新读取不加入旧批次。'}


def expand(c, ctx, db, declaration, prior_body=None):
    """Only replace mechanical receipt lists; layer meaning remains model-owned."""
    token = declaration.get('stage')
    if not token:
        return declaration, None
    if prior_body and prior_body.get('stage_snapshot'):
        # Resume an already persisted review with its original frozen expansion.
        stage = prior_body['stage_snapshot']
        load(c, ctx, db, stage['token'])
        # A frozen retry must not move the review watermark past new business
        # writes. Only the host's own receipts for THIS reply may follow it.
        reply_commit = db.execute('SELECT id FROM commits WHERE document=? AND task=?',
                                  (prior_body['reply']['document'], ctx.task)).fetchone()
        record = 'recording-' + digest((ctx.task+':'+reply_commit['id']).encode())
        mechanical = {record}
        for event in prior_body['events']:
            suffix = digest((record+':'+event).encode())
            mechanical.update(('link-'+suffix, 'delivery-link-'+suffix))
        for row in db.execute("SELECT detail FROM audit WHERE task=? AND id>? AND action='document.committed'",
                              (ctx.task, stage['through_audit'])):
            ident = json.loads(row[0])['commit']
            if ident in mechanical:
                continue
            saved = db.execute('SELECT d.kind FROM commits c JOIN documents d ON d.id=c.document WHERE c.id=?', (ident,)).fetchone()
            if saved and saved['kind'] in ('data','event','context'):
                raise Denied('该答复留存后又有新的业务提交；旧阶段凭据不能覆盖它们。先核对未入队，再以新回复编号完整处理新增记录。')
        return prior_body['resolved_declaration'], stage
    if token == 'current':
        token, body = capture(c, ctx, db)
    else:
        body = load(c, ctx, db, token)
        fresh_token, fresh = capture(c, ctx, db)
        # Later reads are allowed and belong to another candidate batch, but
        # later committed business writes must not disappear from this reply.
        if body['after_audit'] != fresh['after_audit'] or body['receipts'] != fresh['receipts']:
            raise Denied('阶段提交已变化，刷新stage_receipt或使用stage=current。')
    resolved = copy.deepcopy(declaration)
    events = list(resolved.get('events', []))
    state_problems = []
    for kind in ('data','event','context'):
        value = resolved.get(kind)
        if not isinstance(value, dict):
            raise Denied('仍需模型说明各层实际处理与理由。')
        receipts = [r for r in body['receipts'] if r['kind'] == kind]
        # The host saves the incoming message as a data-layer commit and marks it in host_commits (trusted code only). That is not the model's own write,
        # so a data layer whose only commits are host ingestion may honestly say no_change. Any other commit still
        # forbids no_change. The ingestion receipt stays in the stage evidence; only the model's declaration changes.
        trusted = {r[0] for r in db.execute('SELECT id FROM host_commits WHERE task=?', (ctx.task,))} if kind == 'data' else set()
        host_only = kind == 'data' and bool(receipts) and all(r['id'] in trusted for r in receipts)
        if value.get('state') == 'no_change' and receipts and not host_only:
            state_problems.append(kind+'层本阶段已有'+str(len(receipts))+'次成功提交（stage按本阶段累计，不是只看这一次调用；record_report保存的表、前面已写的事项经过都算），state不能是no_change，改为updated并写理由')
            continue
        if value.get('state') == 'updated' and not receipts:
            state_problems.append(kind+'层本阶段没有成功提交，state不能是updated，改为no_change或pending并写理由，或先完成保存')
            continue
        ids = [r['id'] for r in receipts]
        if 'commits' in value:
            listed = value['commits']
            # The program always expands the full list itself, so a hand-written list that only names this
            # layer's own successful commits changes nothing and need not cost a failed call. Anything else is
            # a mistake worth stopping on.
            if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed) or not set(listed) <= set(ids):
                stray = [x for x in listed if x not in ids] if isinstance(listed, list) else listed
                raise ValueError('使用stage时commits由程序展开；你填的编号里有不属于本阶段'+kind+'层成功提交的：'+str(stray)[:200]+'。请省略commits。')
        value['commits'] = [] if (host_only and value.get('state') == 'no_change') else ids
        for r in receipts:
            event = r['document'] if kind == 'event' else r['path'][len('Data/events/'):-3] if r['path'].startswith('Data/events/') else None
            if event and event not in events:
                events.append(event)
    if state_problems:
        correction = {'basis': declaration.get('basis'), 'stage': 'current', 'layers': {}}
        for kind in ('data','event','context'):
            receipts = [r for r in body['receipts'] if r['kind'] == kind]
            trusted = {r[0] for r in db.execute('SELECT id FROM host_commits WHERE task=?',(ctx.task,))} if kind == 'data' else set()
            host_only = bool(receipts) and kind == 'data' and all(r['id'] in trusted for r in receipts)
            correction['layers'][kind] = {'successful_commits':len(receipts), 'allowed_states':(['updated','pending','no_change'] if host_only else ['updated','pending']) if receipts else ['no_change','pending'], 'reason':'由模型按真实处理语义填写，省略commits'}
        raise Denied('使用stage时：'+'；'.join(state_problems)+'。一次核对所有层；commits由宿主展开，无需手填。recording_correction='+json.dumps(correction,ensure_ascii=False,sort_keys=True))
    resolved['events'] = events
    if resolved.get('basis') == 'stage':
        ids = {r['id'] for r in body['receipts']}
        adopted = {}
        for row in db.execute("SELECT b.id,b.body FROM citation_batches b JOIN commits c ON c.id=b.id AND c.task=b.task "
                              "AND c.document=b.event AND c.digest=b.digest WHERE b.task=? AND c.state='committed'", (ctx.task,)):
            if row['id'] not in ids:
                continue
            for use in json.loads(row['body'])['adopted']:
                adopted[(use['id'],use['revision'])] = {'id':use['id'],'revision':use['revision']}
        resolved['basis'] = list(adopted.values())
    return resolved, {'token': token, 'after_audit': body['after_audit'], 'through_audit': body['through_audit']}


def observe(c, ctx, db, snapshot, adopted, reply):
    token, body = (snapshot['token'], load(c, ctx, db, snapshot['token'])) if snapshot else capture(c, ctx, db)
    used = {(r['id'], r['revision']) for r in adopted}
    # Explicitly an upper-bound diagnostic, not semantic correctness checking.
    open_sources = [v['candidate'] for v in body['candidates'] if (v['id'],v['revision']) not in used]
    c.audit(db, ctx.principal, ctx.task, 'stage.shadow',
            {'stage': token, 'reply': reply, 'candidate_count': len(body['candidates']), 'open_sources': open_sources,
             'would_block_if_all_required': bool(open_sources), 'enforced': False,
             'coverage': 'text read/search candidates only; no per-range semantic adjudication or leakage guarantee'})
